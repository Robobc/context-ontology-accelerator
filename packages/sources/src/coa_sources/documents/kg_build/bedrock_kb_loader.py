# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Load pre-chunked text from a Bedrock Managed Knowledge Base as GraphRAG nodes.

Fork addition (``INPUT_MODE=BEDROCK_KB``). The staged-file path re-chunks raw
text with the toolkit's ``SentenceSplitter`` and lets ``IdRewriter`` mint chunk
ids. Here the Knowledge Base already parsed, chunked and embedded the documents,
so this loader hands the toolkit the KB's own chunks and *keeps the KB's chunk
ids*. Two toolkit stages touch ids (graphrag-lexical-graph 3.18.7): the extract
stage's ``IdRewriter._new_id`` leaves any ``aws:`` id alone, and the build
stage's ``NodeBuilders.pre_process`` rewrites segment 1 of every id to the
tenant. Ids are therefore emitted as ``aws::kb:<_chunk_id>`` (empty segment 1)
and stored as ``aws:<tenant>:kb:<_chunk_id>``; the shape lives in
:mod:`coa_common.kb_ids`, shared with serve. A vector hit from Bedrock
``Retrieve`` (``metadata._chunk_id``) therefore maps 1-to-1 onto the
``__Chunk__`` node of the namespace's tenant.

Bedrock has no chunk-enumeration API, so chunks are collected with a
document-scoped ``Retrieve`` scan: a generic query, ``numberOfResults=100``,
paged on ``nextToken``, filtered on ``_data_source_id`` and ``_source_uri``.
Documents come from ``ListKnowledgeBaseDocuments`` where the data source
supports it (S3, CUSTOM); SharePoint data sources reject that call, so the
documents are discovered by sampling ``Retrieve`` until no new document appears
for ``discovery_quiet_pages`` consecutive pages (imperfect by construction; the
caller logs discovered-vs-indexed counts).

Extraction runs as the source's **extraction identity** (``userContext.userId``,
the email of an account the knowledge base owner granted read access to the
documents to extract). It does NOT see every document by right: Bedrock returns
nothing from an ACL-enabled data source to a call without a user context and
offers no administrative bypass (verified live and in the service docs), so the
graph's coverage is exactly that identity's entitlement, which the owner sets.
A scan that lists documents but can read none of them fails with
``ExtractionIdentityRequiredError`` instead of building an empty graph.
Retrieval-time ACL for end users is enforced elsewhere (the serve-side
``BedrockKnowledgeBaseRetriever`` passes the caller's ``userContext``); this
loader never decides what an end user may see.

Output contract (what the toolkit's builders read):
- a ``TextNode`` per chunk, ``id_ = "aws:" + _chunk_id``, ``text`` = the chunk,
  ``relationships[SOURCE]`` = ``RelatedNodeInfo(node_id="aws:doc:<documentId>",
  metadata=<document metadata>)``. The chunk builder MERGEs ``__Chunk__`` on the
  node id and ``__Source__`` on the SOURCE relationship's ``node_id``, copying
  the relationship's ``metadata`` onto the source node (collection-typed values
  are dropped by the toolkit, so all values here are scalars).
- a ``Document`` per source document (``id_ = "aws:doc:<documentId>"``, empty
  text) is returned alongside for callers that want the document list; the
  chunks alone are what the pipeline needs.
- raw identifiers are kept as scalar properties (``bedrockChunkId`` on the
  chunk via ``chunk_external_properties``-compatible metadata,
  ``bedrockDocumentId`` on the source) so joins never depend on prefix
  stripping.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import structlog
from coa_common.kb_ids import emit_chunk_id, emit_document_id

logger = structlog.get_logger(__name__)

RETRIEVE_PAGE_SIZE = 100  # Bedrock's maximum numberOfResults per Retrieve call
DISCOVERY_QUERY = "document"  # a query string is required; the filter does the scoping
# Bedrock system metadata keys that are promoted to named properties; every
# other ``_``-prefixed key is dropped (reserved to the service).
_SYSTEM_KEY_MAP = {
    "_source_uri": "source_uri",
    "_document_title": "document_title",
    "_data_source_id": "data_source_id",
    "_excerpt_page_number": "page_number",
    "_last_updated_at": "last_updated_at",
}
# ``_chunk_id`` and ``documentId`` are handled explicitly, never copied as-is.
_RESERVED_TOOLKIT_KEYS = frozenset({"source", "chunk", "topics", "chunkId", "sourceId"})


@dataclass
class KbDocument:
    """One Knowledge Base document as discovered for extraction."""

    document_id: str
    data_source_id: str
    source_uri: str = ""
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class KbLoadResult:
    """What the loader produced, plus the counters the caller reports."""

    documents: list  # llama_index Document per source document
    chunks: list  # llama_index TextNode per KB chunk, grouped per document
    manifest: dict[str, set[str]]  # documentId -> set(_chunk_id); input to the incremental diff
    documents_discovered: int = 0
    documents_without_chunks: int = 0
    # Listed by the KB but not returned to the extraction identity (ACL-enabled
    # data source, identity granted some documents and not others).
    documents_not_visible: int = 0
    retrieve_calls: int = 0
    duplicate_chunks_skipped: int = 0


def sanitize_metadata_keys(
    items: Iterable[tuple[str, Any]], *, on_collision: Callable[..., None] | None = None
) -> dict:
    """Return ``{safe_key: value}`` with Neptune/OpenCypher-safe keys.

    Dots and hyphens become underscores (the toolkit embeds metadata keys in
    Cypher unquoted). Two distinct keys that collapse onto the same safe key are
    disambiguated with a numeric suffix so no value is silently dropped; the
    optional ``on_collision(original_key=, collides_with=, sanitized_key=)`` hook
    lets the caller log it. Shared by the staged-file loader and this one.
    """
    out: dict[str, Any] = {}
    origin_of: dict[str, str] = {}
    for k, v in items:
        safe_key = k.replace(".", "_").replace("-", "_")
        if safe_key in origin_of and origin_of[safe_key] != k:
            if on_collision is not None:
                on_collision(original_key=k, collides_with=origin_of[safe_key], sanitized_key=safe_key)
            suffix = 2
            while f"{safe_key}_{suffix}" in origin_of:
                suffix += 1
            safe_key = f"{safe_key}_{suffix}"
        origin_of[safe_key] = k
        out[safe_key] = v
    return out


def chunk_node_id(bedrock_chunk_id: str) -> str:
    """``aws::kb:<_chunk_id>``; the toolkit's tenant rewrite stores it as ``aws:<tenant>:kb:<_chunk_id>``.

    The shape is owned by :mod:`coa_common.kb_ids`, shared with serve and the
    ontology engine; see there for why the empty second segment matters.
    """
    return emit_chunk_id(bedrock_chunk_id)


def document_node_id(document_id: str) -> str:
    """``aws::doc:<tail>``; stored as ``aws:<tenant>:doc:<tail>``. See :mod:`coa_common.kb_ids`."""
    return emit_document_id(document_id)


def _public_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Bedrock result metadata -> scalar, sanitised node metadata.

    System keys in ``_SYSTEM_KEY_MAP`` are renamed; other ``_``-prefixed keys
    are dropped; customer keys are kept (sanitised). Collection values are
    dropped because the toolkit refuses them on source nodes.
    """
    if not raw:
        return {}
    items: list[tuple[str, Any]] = []
    for k, v in raw.items():
        if isinstance(v, (list, dict, set, tuple)):
            continue
        if k.startswith("_"):
            mapped = _SYSTEM_KEY_MAP.get(k)
            if mapped is None:
                continue
            items.append((mapped, v))
        elif k in _RESERVED_TOOLKIT_KEYS:
            continue
        else:
            items.append((k, v))
    return sanitize_metadata_keys(items)


def _filter(data_source_id: str, source_uri: str | None = None) -> dict[str, Any]:
    ds = {"equals": {"key": "_data_source_id", "value": data_source_id}}
    if source_uri is None:
        return ds
    return {"andAll": [ds, {"equals": {"key": "_source_uri", "value": source_uri}}]}


def _user_context(extraction_user_id: str | None) -> dict[str, Any]:
    """The ``userContext`` kwarg for the extraction identity, or nothing.

    Bedrock applies a data source's ACL to ``Retrieve`` and ``GetDocumentContent``
    by the ``userContext.userId`` on the call and returns NOTHING from an
    ACL-enabled data source to a call without one; there is no administrative
    bypass. The identity is therefore what the graph can see, and the knowledge
    base owner controls coverage through that one account's permissions. A
    non-ACL data source returns every document to any identity, so passing it
    always is harmless.
    """
    return {"userContext": {"userId": extraction_user_id}} if extraction_user_id else {}


class ExtractionIdentityRequiredError(RuntimeError):
    """A data source listed documents, but the scan could read none of their chunks.

    That is the signature of an ACL-enabled data source read without an
    extraction identity (or with one the knowledge base owner has not granted
    access): the documents exist, ``Retrieve`` returns none of them, and nothing
    else distinguishes it from an empty data source. Raised so the scan fails
    rather than build an empty graph that looks like a successful one.
    """


def _retrieve_pages(
    runtime: Any,
    *,
    knowledge_base_id: str,
    retrieval_filter: dict[str, Any],
    result: KbLoadResult,
    max_pages: int | None = None,
    backoff_s: float = 1.0,
    extraction_user_id: str | None = None,
) -> Iterator[list[dict[str, Any]]]:
    """Page through ``Retrieve`` for one filter; yields each page's results.

    Backs off on ``ThrottlingException`` (doubling, capped at 30 s, 6 attempts)
    rather than failing the whole document.
    """
    token: str | None = None
    pages = 0
    while True:
        kwargs: dict[str, Any] = {
            "knowledgeBaseId": knowledge_base_id,
            "retrievalQuery": {"text": DISCOVERY_QUERY},
            "retrievalConfiguration": {
                "managedSearchConfiguration": {
                    "numberOfResults": RETRIEVE_PAGE_SIZE,
                    "filter": retrieval_filter,
                }
            },
            **_user_context(extraction_user_id),
        }
        if token:
            kwargs["nextToken"] = token
        delay = backoff_s
        for attempt in range(6):
            try:
                resp = runtime.retrieve(**kwargs)
                break
            except Exception as exc:  # botocore ClientError without importing botocore here
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
                if code != "ThrottlingException" or attempt == 5:
                    raise
                logger.warning("bedrock_kb_retrieve_throttled", attempt=attempt + 1, sleep_s=delay)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        result.retrieve_calls += 1
        pages += 1
        yield resp.get("retrievalResults", []) or []
        token = resp.get("nextToken")
        if not token or (max_pages is not None and pages >= max_pages):
            return


def discover_documents(
    agent: Any,
    runtime: Any,
    *,
    knowledge_base_id: str,
    data_source_id: str,
    result: KbLoadResult,
    discovery_quiet_pages: int = 3,
    max_discovery_pages: int = 200,
    extraction_user_id: str | None = None,
) -> list[KbDocument]:
    """Documents of one data source: ``ListKnowledgeBaseDocuments`` when it works, else ``Retrieve`` sampling.

    The list API is document-level and rejects SharePoint data sources; for those
    the Retrieve scan collects distinct ``documentId`` values until
    ``discovery_quiet_pages`` consecutive pages add nothing new. The S3 listing
    yields the ``documentId`` (an ``s3://`` URI) but not the ``_source_uri`` the
    KB filters on (an ``https://`` URL for S3), so on that path each listed
    document is located through the data-source-wide scan by ``documentId`` and
    its ``_source_uri`` and title are read off its first result.

    The listing is a control-plane call and names every document regardless of
    ACL; the scan is subject to it. Listed documents that the scan cannot see at
    all are the ACL-without-identity signature and raise
    :class:`ExtractionIdentityRequiredError` (see there).
    """
    docs: dict[str, KbDocument] = {}

    listed_uris: list[str] | None = None
    try:
        listed_uris = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"knowledgeBaseId": knowledge_base_id, "dataSourceId": data_source_id}
            if token:
                kwargs["nextToken"] = token
            resp = agent.list_knowledge_base_documents(**kwargs)
            for d in resp.get("documentDetails", []) or []:
                ident = d.get("identifier") or {}
                uri = (ident.get("s3") or {}).get("uri") or (ident.get("custom") or {}).get("id")
                if uri and d.get("status", "INDEXED") in ("INDEXED", "PARTIALLY_INDEXED"):
                    listed_uris.append(uri)
            token = resp.get("nextToken")
            if not token:
                break
        logger.info("bedrock_kb_documents_listed", data_source_id=data_source_id, count=len(listed_uris))
    except Exception as exc:
        # Only "this data source type does not support the call" (SharePoint and
        # friends, a ValidationException) falls back to sampling. Anything else,
        # an AccessDenied above all, is a real failure: sampling would then run
        # on the same credentials and either fail too or, worse, succeed and
        # hide that the listing path is broken.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        if code != "ValidationException":
            raise
        logger.info("bedrock_kb_list_documents_unavailable", data_source_id=data_source_id, error=code)
        listed_uris = None

    quiet = 0
    for page in _retrieve_pages(
        runtime,
        knowledge_base_id=knowledge_base_id,
        retrieval_filter=_filter(data_source_id),
        result=result,
        # The listed path knows when it is done (every listed document seen) and
        # must not be capped, or a large data source reports phantom invisibility.
        max_pages=None if listed_uris is not None else max_discovery_pages,
        extraction_user_id=extraction_user_id,
    ):
        before = len(docs)
        for r in page:
            _note_document(docs, r, data_source_id)
        quiet = quiet + 1 if len(docs) == before else 0
        if listed_uris is not None:
            # Listed path: stop as soon as every listed document has been seen.
            if all(uri in docs for uri in listed_uris):
                break
        elif quiet >= discovery_quiet_pages:
            break

    if listed_uris is not None:
        unseen = [uri for uri in listed_uris if uri not in docs]
        if listed_uris and len(unseen) == len(listed_uris):
            raise ExtractionIdentityRequiredError(
                f"data source {data_source_id} lists {len(listed_uris)} indexed document(s) but Retrieve "
                f"returned none of them: the data source is ACL-enabled and the scan "
                + (
                    "ran without an extraction identity"
                    if not extraction_user_id
                    else f"ran as {extraction_user_id!r}, which has no access to them"
                )
                + ". Set bedrockKnowledgeBaseConfiguration.extractionUserId to an account the knowledge "
                "base owner granted read access to the documents to extract."
            )
        if unseen:
            # Partial visibility: the identity is granted some documents, not all.
            # Coverage is the owner's call; report it rather than guess.
            result.documents_not_visible += len(unseen)
            logger.warning(
                "bedrock_kb_documents_not_visible_to_extraction_identity",
                data_source_id=data_source_id,
                listed=len(listed_uris),
                visible=len(listed_uris) - len(unseen),
                extraction_user_id=extraction_user_id or "",
            )
        logger.info("bedrock_kb_documents_located", data_source_id=data_source_id, count=len(docs))
    else:
        logger.info("bedrock_kb_documents_sampled", data_source_id=data_source_id, count=len(docs), quiet_pages=quiet)
    return list(docs.values())


def _note_document(docs: dict[str, KbDocument], r: dict[str, Any], data_source_id: str) -> None:
    doc_id = r.get("documentId")
    if not doc_id or doc_id in docs:
        return
    md = r.get("metadata") or {}
    docs[doc_id] = KbDocument(
        document_id=doc_id,
        data_source_id=data_source_id,
        source_uri=str(md.get("_source_uri") or ""),
        title=str(md.get("_document_title") or ""),
        metadata=_public_metadata(md),
    )


def load_bedrock_kb_chunks(
    *,
    knowledge_base_id: str,
    data_source_ids: list[str] | None,
    runtime: Any,
    agent: Any,
    on_collision: Callable[..., None] | None = None,
    extraction_user_id: str | None = None,
) -> KbLoadResult:
    """Collect every chunk of the selected data sources as toolkit nodes.

    ``runtime`` is a ``bedrock-agent-runtime`` client and ``agent`` a
    ``bedrock-agent`` client, both already assuming the extraction role. When
    ``data_source_ids`` is empty or ``None`` every data source of the KB is used.
    Chunks are emitted grouped per document (the extraction pipeline groups
    consecutive nodes by their SOURCE relationship) and de-duplicated on
    ``_chunk_id`` within a document. ``extraction_user_id`` is the identity every
    ``Retrieve`` runs as (see :func:`_user_context`); what it may read is what
    the graph will contain.
    """
    from llama_index.core.schema import Document, NodeRelationship, RelatedNodeInfo, TextNode

    result = KbLoadResult(documents=[], chunks=[], manifest={})

    if not data_source_ids:
        data_source_ids = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"knowledgeBaseId": knowledge_base_id}
            if token:
                kwargs["nextToken"] = token
            resp = agent.list_data_sources(**kwargs)
            data_source_ids.extend(d["dataSourceId"] for d in resp.get("dataSourceSummaries", []) or [])
            token = resp.get("nextToken")
            if not token:
                break

    for ds in data_source_ids:
        kb_docs = discover_documents(
            agent,
            runtime,
            knowledge_base_id=knowledge_base_id,
            data_source_id=ds,
            result=result,
            extraction_user_id=extraction_user_id,
        )
        result.documents_discovered += len(kb_docs)
        for kb_doc in kb_docs:
            doc_node_id = document_node_id(kb_doc.document_id)
            source_metadata: dict[str, Any] = {
                "bedrockDocumentId": kb_doc.document_id,
                "knowledgeBaseId": knowledge_base_id,
                "data_source_id": ds,
            }
            if kb_doc.source_uri:
                source_metadata["source_uri"] = kb_doc.source_uri
                source_metadata["filename"] = kb_doc.source_uri.rsplit("/", 1)[-1]
            if kb_doc.title:
                source_metadata["document_title"] = kb_doc.title
            for k, v in kb_doc.metadata.items():
                source_metadata.setdefault(k, v)
            source_metadata = sanitize_metadata_keys(source_metadata.items(), on_collision=on_collision)

            seen: set[str] = set()
            doc_chunks: list = []
            for page in _retrieve_pages(
                runtime,
                knowledge_base_id=knowledge_base_id,
                retrieval_filter=_filter(ds, kb_doc.source_uri) if kb_doc.source_uri else _filter(ds),
                result=result,
                extraction_user_id=extraction_user_id,
            ):
                for r in page:
                    if kb_doc.source_uri == "" and r.get("documentId") != kb_doc.document_id:
                        continue  # unscoped scan: keep only this document's chunks
                    md = r.get("metadata") or {}
                    cid = md.get("_chunk_id")
                    text = (r.get("content") or {}).get("text")
                    if not cid or not isinstance(text, str) or not text.strip():
                        continue
                    if cid in seen:
                        result.duplicate_chunks_skipped += 1
                        continue
                    seen.add(cid)
                    chunk_metadata = {"bedrockChunkId": cid, **_public_metadata(md)}
                    chunk_metadata.pop("source_uri", None)  # lives on the source node
                    chunk_metadata.pop("document_title", None)
                    node = TextNode(id_=chunk_node_id(cid), text=text, metadata=chunk_metadata)
                    node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(
                        node_id=doc_node_id, metadata=dict(source_metadata)
                    )
                    node.excluded_llm_metadata_keys = list(chunk_metadata.keys())
                    node.excluded_embed_metadata_keys = list(chunk_metadata.keys())
                    doc_chunks.append(node)

            if not doc_chunks:
                result.documents_without_chunks += 1
                logger.warning("bedrock_kb_document_has_no_chunks", document_id=kb_doc.document_id, data_source_id=ds)
                continue
            result.manifest[kb_doc.document_id] = seen
            result.chunks.extend(doc_chunks)
            result.documents.append(Document(id_=doc_node_id, text="", metadata=dict(source_metadata)))

    logger.info(
        "bedrock_kb_chunks_loaded",
        knowledge_base_id=knowledge_base_id,
        data_sources=len(data_source_ids),
        documents=len(result.documents),
        documents_discovered=result.documents_discovered,
        documents_without_chunks=result.documents_without_chunks,
        documents_not_visible=result.documents_not_visible,
        extraction_user_id=extraction_user_id or "",
        chunks=len(result.chunks),
        duplicate_chunks_skipped=result.duplicate_chunks_skipped,
        retrieve_calls=result.retrieve_calls,
    )
    return result


def coverage_ratio(
    runtime: Any,
    *,
    knowledge_base_id: str,
    data_source_id: str,
    document_id: str,
    chunk_texts: Iterable[str],
    extraction_user_id: str | None = None,
) -> float:
    """Share of chunks that are verbatim substrings of the KB's pre-chunking text.

    Uses ``GetDocumentContent(outputFormat=EXTRACTED)`` (a 5-minute presigned URL
    to ``{nonPlainTextDocumentContent:[{text,...}]}``), whitespace-normalised on
    both sides. Optional completeness check; a ratio under 0.99 means the scan
    missed chunks or the KB re-chunked since the scan. Runs as the extraction
    identity: an ACL-enabled data source refuses the call without one.
    """
    import json
    import urllib.request

    resp = runtime.get_document_content(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
        documentId=document_id,
        outputFormat="EXTRACTED",
        **_user_context(extraction_user_id),
    )
    with urllib.request.urlopen(resp["presignedUrl"], timeout=60) as fh:  # noqa: S310 - AWS presigned URL
        payload = json.loads(fh.read())
    full = " ".join(" ".join(str(e.get("text", "")).split()) for e in payload.get("nonPlainTextDocumentContent", []))
    texts = list(chunk_texts)
    if not texts:
        return 0.0
    hits = sum(1 for t in texts if " ".join(t.split()) in full)
    return hits / len(texts)
