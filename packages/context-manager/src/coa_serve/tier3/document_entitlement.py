# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Document-level entitlement for graph-derived text (fork, Bedrock Managed KB).

When a namespace's documents live in a Bedrock Knowledge Base with document-level
ACL, ``Retrieve`` already answers per user. The lexical graph does not: it was
extracted under the extraction role and holds every document's chunks, facts,
statements and entities, so a graph hop (adjacent chunks, an entity's supporting
statements, an entity label matched by keyword, a lexical-baseline traversal)
would show a user text from a document the KB would never have returned to them.

This gate closes those hops. Every piece of graph content has provenance to a
``__Source__`` node (``aws:doc:<documentId>``, property ``source_uri``). Before
content is returned, the gate resolves the distinct source documents behind it
with one openCypher query and asks the KB, per document, whether *this* caller
may read it (``BedrockKnowledgeBaseRetriever.probe_source_uris``: one filtered
``Retrieve`` with ``userContext``). Content whose document is not entitled, or
whose provenance cannot be resolved, is dropped (fail closed). A probe the KB
could not answer (throttled, unavailable) is also treated as not entitled for
this request but is **not** cached, so a transient fault never becomes a 60 s
negative answer.

Four filters, one per shape of graph content:

* :meth:`DocumentEntitlementGate.filter_chunks` -- ``__Chunk__`` text by chunk id
  (chunk-window expansion).
* :meth:`DocumentEntitlementGate.filter_statement_chunks` -- ``__Statement__``
  text returned per entity (graph traversal).
* :meth:`DocumentEntitlementGate.filter_entities` -- ``__Entity__`` labels (graph
  traversal, keyword mode included). A label is document-derived text too.
* :meth:`DocumentEntitlementGate.filter_lexical_nodes` -- toolkit result nodes of
  the lexical-baseline retriever and the agentic ``strategy:*`` tools, by the
  ``__Source__`` id they carry.

Scope: active only for namespaces whose document retriever is a per-user Bedrock
KB (``enforces_user_context``); everything else passes through untouched. Only
KB-sourced content (chunk ids ``aws:...``, source ids ``aws:doc:...``) is under
the KB's ACL; content the toolkit minted itself passes through, logged.

Not covered here, by design (see ADR 0011): the SPARQL *ontology* graph read by
``graphTraverse`` and the non-agentic ``KnowledgeRetriever``. It holds induced
classes and properties, not document text, and is authorized at namespace level;
unstructured induction over a per-user KB's lexical graph is not run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog

from ..request_identity import get_request_user
from .graph_traverser import GraphEntity
from .vector_retriever import ChunkResult

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coa-entitlement")
KB_CHUNK_ID_PREFIX = "aws:"
KB_SOURCE_ID_PREFIX = "aws:doc:"

# Chunk -> source document. Chunks are keyed by Neptune node id (the toolkit's
# ``chunkId`` property is null on the dev graph; see chunk_tool schema notes).
CHUNK_PROVENANCE: str = (
    "MATCH (c:{chunk_label})-[:`__EXTRACTED_FROM__`]->(s:{source_label})\n"
    "WHERE id(c) IN $chunk_ids\n"
    "RETURN id(c) AS chunkId, s.source_uri AS uri"
)
# Entity -> supporting statement text -> chunk -> source document. Mirrors the
# text hop of EDGE_TYPED_TRAVERSAL (entity -SUBJECT|OBJECT-> fact -SUPPORTS->
# statement) and continues statement -MENTIONED_IN-> chunk -EXTRACTED_FROM-> source.
# Edge directions verified against graphrag-lexical-graph 3.18.7 build/*.py.
STATEMENT_PROVENANCE: str = (
    "MATCH (e:{entity_label})-[:`__SUBJECT__`|`__OBJECT__`]->()-[:`__SUPPORTS__`]->(st:{statement_label})\n"
    "WHERE id(e) IN $entity_ids\n"
    "MATCH (st)-[:`__MENTIONED_IN__`]->(c:{chunk_label})-[:`__EXTRACTED_FROM__`]->(s:{source_label})\n"
    "RETURN id(e) AS entityId, st.value AS text, s.source_uri AS uri"
)
# Entity -> every source document it was extracted from (same path, no text).
ENTITY_PROVENANCE: str = (
    "MATCH (e:{entity_label})-[:`__SUBJECT__`|`__OBJECT__`]->()-[:`__SUPPORTS__`]->(st:{statement_label})\n"
    "WHERE id(e) IN $entity_ids\n"
    "MATCH (st)-[:`__MENTIONED_IN__`]->(:{chunk_label})-[:`__EXTRACTED_FROM__`]->(s:{source_label})\n"
    "RETURN DISTINCT id(e) AS entityId, s.source_uri AS uri"
)
# Source node id -> its document URI (lexical-baseline results name the source).
SOURCE_PROVENANCE: str = (
    "MATCH (s:{source_label})\nWHERE id(s) IN $source_ids\nRETURN id(s) AS sourceId, s.source_uri AS uri"
)


class DocumentEntitlementGate:
    """Filter graph-derived content down to documents the caller may read."""

    def __init__(
        self,
        graph_store_uri: str,
        kb_retriever_for: Callable[[str], Any | None],
        *,
        query_timeout_s: float = 15.0,
        cache_ttl_s: float = 60.0,
        cache_max_entries: int = 10_000,
    ) -> None:
        """``kb_retriever_for(namespace)`` returns that namespace's Bedrock KB retriever or ``None``."""
        self._graph_store_uri = graph_store_uri
        self._kb_retriever_for = kb_retriever_for
        self._query_timeout_s = query_timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._cache_max_entries = max(1, cache_max_entries)
        self._store: Any = None
        # (caller, namespace, uri) -> (entitled, expiry). Only definite answers
        # are cached; an unresolved probe is retried on the next request.
        self._cache: dict[tuple[str, str, str], tuple[bool, float]] = {}

    # ── applicability ─────────────────────────────────────────────────────

    def _retriever(self, namespace: str) -> Any | None:
        retriever = self._kb_retriever_for(namespace)
        if retriever is None or not getattr(retriever, "enforces_user_context", False):
            return None
        return retriever

    def applies_to(self, namespace: str) -> bool:
        """True when ``namespace`` reads documents from a per-user Bedrock KB."""
        return self._retriever(namespace) is not None

    # ── graph access (lazy toolkit import, same pattern as the tools) ────────

    def _get_store(self) -> Any:
        if self._store is None:
            from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory  # lazy

            self._store = GraphStoreFactory.for_graph_store(self._graph_store_uri)
        return self._store

    @staticmethod
    def _label(namespace: str, name: str) -> str:
        from coa_common.constants import to_graphrag_tenant_id
        from graphrag_toolkit.lexical_graph import TenantId  # lazy

        return TenantId(to_graphrag_tenant_id(namespace)).format_label(name)

    def _labels(self, namespace: str) -> dict[str, str]:
        return {
            "chunk_label": self._label(namespace, "__Chunk__"),
            "source_label": self._label(namespace, "__Source__"),
            "entity_label": self._label(namespace, "__Entity__"),
            "statement_label": self._label(namespace, "__Statement__"),
        }

    async def _run_cypher(self, cypher: str, params: dict) -> list[dict]:
        store = self._get_store()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_EXECUTOR, lambda: store.execute_query_with_retry(cypher, params))
        rows = await asyncio.wait_for(asyncio.shield(fut), timeout=self._query_timeout_s)
        return list(rows or [])

    # ── entitlement ─────────────────────────────────────────────────────

    def _cache_put(self, key: tuple[str, str, str], entitled: bool, expiry: float) -> None:
        if len(self._cache) >= self._cache_max_entries:
            now = time.monotonic()
            for k in [k for k, (_, exp) in self._cache.items() if exp <= now]:
                del self._cache[k]
            while len(self._cache) >= self._cache_max_entries:  # still full: evict oldest inserted
                del self._cache[next(iter(self._cache))]
        self._cache[key] = (entitled, expiry)

    async def entitled_uris(self, namespace: str, uris: Iterable[str], user_id: str | None = None) -> set[str]:
        """Subset of ``uris`` the caller may read; definite answers cached per (user, namespace, uri).

        A URI the KB could not answer for (throttled, unavailable) is not in the
        result and not cached; it is logged as ``unresolved`` so an operator can
        tell a denial from an outage.
        """
        retriever = self._retriever(namespace)
        caller = (user_id or "").strip() or get_request_user()
        wanted = {u for u in uris if u}
        if retriever is None or not caller or not wanted:
            return set()
        now = time.monotonic()
        entitled: set[str] = set()
        to_ask: set[str] = set()
        for uri in wanted:
            hit = self._cache.get((caller, namespace, uri))
            if hit is not None and hit[1] > now:
                if hit[0]:
                    entitled.add(uri)
            else:
                to_ask.add(uri)
        if to_ask:
            answers: dict[str, bool | None] = await retriever.probe_source_uris(to_ask, user_id=caller)
            expiry = now + self._cache_ttl_s
            unresolved = 0
            for uri in to_ask:
                verdict = answers.get(uri)
                if verdict is None:
                    unresolved += 1
                    continue
                self._cache_put((caller, namespace, uri), verdict, expiry)
                if verdict:
                    entitled.add(uri)
            if unresolved:
                logger.warning("document_entitlement_unresolved", namespace=namespace, documents=unresolved)
        return entitled

    async def filter_chunks(
        self, chunks: list[ChunkResult], namespace: str, user_id: str | None = None
    ) -> list[ChunkResult]:
        """Keep only KB-sourced chunks whose document the caller may read.

        Chunks not minted from the KB (ids without the ``aws:`` prefix) pass
        through: they are not under the KB's ACL. Returns ``chunks`` unchanged
        when the gate does not apply to ``namespace``.
        """
        if not chunks or not self.applies_to(namespace):
            return chunks
        kb_ids = [c.chunk_id for c in chunks if c.chunk_id.startswith(KB_CHUNK_ID_PREFIX)]
        self._log_passthrough(namespace, "chunks", len(chunks) - len(kb_ids))
        if not kb_ids:
            return chunks
        rows = await self._run_cypher(CHUNK_PROVENANCE.format(**self._labels(namespace)), {"chunk_ids": kb_ids})
        uri_of = {str(r.get("chunkId")): str(r.get("uri") or "") for r in rows}
        allowed = await self.entitled_uris(namespace, uri_of.values(), user_id)
        kept = [c for c in chunks if not c.chunk_id.startswith(KB_CHUNK_ID_PREFIX) or uri_of.get(c.chunk_id) in allowed]
        self._log_dropped(namespace, "chunks", len(chunks), len(kept))
        return kept

    async def filter_statement_chunks(
        self, chunks: list[ChunkResult], namespace: str, user_id: str | None = None
    ) -> list[ChunkResult]:
        """Keep only statement texts (``uri`` = entity id) from documents the caller may read.

        The graph tool returns an entity's supporting statements as chunks with
        ``uri`` set to the entity id and ``text`` to the statement; a statement
        may be supported from several documents, and one entitled document is
        enough. Statements with no resolvable provenance are dropped.
        """
        if not chunks or not self.applies_to(namespace):
            return chunks
        entity_ids = sorted({c.uri for c in chunks if c.uri})
        if not entity_ids:
            return []
        rows = await self._run_cypher(
            STATEMENT_PROVENANCE.format(**self._labels(namespace)), {"entity_ids": entity_ids}
        )
        uris_for: dict[tuple[str, str], set[str]] = {}
        for r in rows:
            uris_for.setdefault((str(r.get("entityId")), str(r.get("text") or "")), set()).add(str(r.get("uri") or ""))
        allowed = await self.entitled_uris(namespace, (u for s in uris_for.values() for u in s), user_id)
        kept = [c for c in chunks if uris_for.get((c.uri, c.text), set()) & allowed]
        self._log_dropped(namespace, "statements", len(chunks), len(kept))
        return kept

    async def filter_entities(
        self, entities: Iterable[GraphEntity], namespace: str, user_id: str | None = None
    ) -> list[GraphEntity]:
        """Keep only entities extracted from at least one document the caller may read.

        An entity label ("Project Falcon") is document-derived text; one it was
        extracted from only in documents this user may not read is withheld
        along with its relationships. Entities with no resolvable provenance are
        dropped.
        """
        items = list(entities)
        if not items or not self.applies_to(namespace):
            return items
        entity_ids = sorted({e.uri for e in items if e.uri})
        if not entity_ids:
            return []
        rows = await self._run_cypher(ENTITY_PROVENANCE.format(**self._labels(namespace)), {"entity_ids": entity_ids})
        uris_for: dict[str, set[str]] = {}
        for r in rows:
            uris_for.setdefault(str(r.get("entityId")), set()).add(str(r.get("uri") or ""))
        allowed = await self.entitled_uris(namespace, (u for s in uris_for.values() for u in s), user_id)
        kept = [e for e in items if uris_for.get(e.uri, set()) & allowed]
        self._log_dropped(namespace, "entities", len(items), len(kept))
        return kept

    async def filter_lexical_nodes(self, nodes: list[Any], namespace: str, user_id: str | None = None) -> list[Any]:
        """Keep only toolkit result nodes whose ``__Source__`` document the caller may read.

        ``nodes`` are the ``NodeWithScore`` results of a graphrag-toolkit
        retrieval (lexical-baseline path, agentic ``strategy:*`` tools); each
        names its source document under ``metadata.source.sourceId`` (the
        ``__Source__`` node id, ``aws:doc:...`` for KB documents). Nodes from a
        source the toolkit minted itself pass through; nodes that name no source
        at all are dropped when the gate applies (their text cannot be placed).
        """
        if not nodes or not self.applies_to(namespace):
            return nodes
        from ..lexical.baseline_retriever import _extract_source_doc  # lazy: avoids an import cycle

        source_of = {id(n): _extract_source_doc(getattr(n, "metadata", None) or {}) for n in nodes}
        kb_sources = sorted({s for s in source_of.values() if s.startswith(KB_SOURCE_ID_PREFIX)})
        self._log_passthrough(
            namespace, "lexical", sum(1 for s in source_of.values() if s and not s.startswith(KB_SOURCE_ID_PREFIX))
        )
        uri_of: dict[str, str] = {}
        if kb_sources:
            rows = await self._run_cypher(
                SOURCE_PROVENANCE.format(**self._labels(namespace)), {"source_ids": kb_sources}
            )
            uri_of = {str(r.get("sourceId")): str(r.get("uri") or "") for r in rows}
        allowed = await self.entitled_uris(namespace, uri_of.values(), user_id)
        kept = []
        for n in nodes:
            source = source_of[id(n)]
            if not source:
                continue  # unplaceable text: fail closed
            if not source.startswith(KB_SOURCE_ID_PREFIX) or uri_of.get(source) in allowed:
                kept.append(n)
        self._log_dropped(namespace, "lexical", len(nodes), len(kept))
        return kept

    @staticmethod
    def _log_dropped(namespace: str, kind: str, before: int, after: int) -> None:
        if after < before:
            logger.info("document_entitlement_filtered", namespace=namespace, kind=kind, before=before, after=after)

    @staticmethod
    def _log_passthrough(namespace: str, kind: str, count: int) -> None:
        if count:
            logger.info("document_entitlement_passthrough_non_kb", namespace=namespace, kind=kind, count=count)


def kb_retriever_resolver(document_retriever: Any) -> Callable[[str], Any | None] | None:
    """``namespace -> per-user Bedrock KB retriever`` for the service's document retriever, or ``None``.

    Understands the two shapes ``build_document_retriever`` produces: a bare
    ``BedrockKnowledgeBaseRetriever`` (every namespace) and a
    ``RoutingDocumentRetriever`` (per-namespace routes plus a default). Returns
    ``None`` when no per-user KB is configured at all, so callers can skip
    building a gate.
    """
    from .bedrock_kb_retriever import BedrockKnowledgeBaseRetriever

    def _per_user(r: Any) -> Any | None:
        return r if isinstance(r, BedrockKnowledgeBaseRetriever) and r.enforces_user_context else None

    resolve = getattr(document_retriever, "_resolve", None)
    if callable(resolve):
        candidates = list(getattr(document_retriever, "_routes", {}).values()) + [
            getattr(document_retriever, "_default", None)
        ]
        if not any(_per_user(c) for c in candidates):
            return None
        return lambda namespace: _per_user(resolve(namespace))
    if _per_user(document_retriever) is not None:
        return lambda namespace: document_retriever
    return None


def build_document_entitlement_gate(graph_store_uri: str, document_retriever: Any) -> DocumentEntitlementGate | None:
    """The one gate every consumer shares, or ``None`` when no per-user KB is configured.

    Built once at startup in ``main.py`` and handed to the lexical-baseline
    retriever, the agentic tool registry and the chunk/graph tools, so they share
    a graph store and an entitlement cache.
    """
    if not graph_store_uri or document_retriever is None:
        return None
    resolver = kb_retriever_resolver(document_retriever)
    if resolver is None:
        return None
    logger.info("document_entitlement_gate_enabled")
    return DocumentEntitlementGate(graph_store_uri, resolver)
