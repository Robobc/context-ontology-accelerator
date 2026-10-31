# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Amazon Bedrock Knowledge Bases document retriever."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import boto3
import structlog
from botocore.config import Config as BotoConfig
from coa_common import resolve_region
from coa_common.constants import to_graphrag_tenant_id
from coa_common.kb_ids import stored_chunk_id

from ..clients.base import instrumented
from ..request_identity import get_request_user
from .vector_retriever import _MAX_TOP_K, DEFAULT_TOP_K, ChunkResult

logger = structlog.get_logger(__name__)

_SOURCE_METADATA_KEYS = (
    "_source_uri",  # Bedrock system key (managed and vector KBs)
    "x-amz-bedrock-kb-source-uri",
    "source_uri",
    "source",
    "document_id",
)
_CHUNK_METADATA_KEYS = (
    "_chunk_id",  # Bedrock system key; stable, content-derived (observed, not contractual)
    "x-amz-bedrock-kb-chunk-id",
    "chunk_id",
)
_LABEL_METADATA_KEYS = ("_document_title", "title", "document_title", "name")

# Chunk ids carried into the graph by kg_build/bedrock_kb_loader.py are the KB's
# own ids in the shape coa_common.kb_ids owns (``aws:<tenant>:kb:<_chunk_id>``
# once the toolkit has rewritten them for the namespace's tenant), so a Retrieve
# hit and its __Chunk__ node share one identifier.


def _first_string(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _location_uri(value: Any) -> str:
    """Find the first URI/URL carried by a Bedrock retrieval location."""
    if isinstance(value, dict):
        for key in ("uri", "url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for nested in value.values():
            candidate = _location_uri(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _location_uri(nested)
            if candidate:
                return candidate
    return ""


def _label_from_uri(uri: str) -> str:
    if not uri:
        return ""
    path = urlparse(uri).path
    return PurePosixPath(path).name


class BedrockKnowledgeBaseRetriever:
    """Retrieve passages using Bedrock's text-first ``Retrieve`` API."""

    def __init__(
        self,
        knowledge_base_id: str,
        *,
        region: str | None = None,
        client: Any | None = None,
        search_mode: str = "vector",
        enforce_user_context: bool = False,
        data_source_ids: Iterable[str] | None = None,
    ) -> None:
        """Configure one existing Knowledge Base and an optional test client.

        ``search_mode`` is ``"vector"`` (``vectorSearchConfiguration``, the
        default and today's behaviour) or ``"managed"`` (Bedrock Managed Knowledge
        Bases, ``managedSearchConfiguration``). With ``enforce_user_context`` every
        ``Retrieve`` carries ``userContext.userId`` for the request's caller so the
        KB applies its document-level ACL; a request with no caller then returns
        no chunks rather than everyone's. ``data_source_ids`` scopes every
        ``Retrieve`` (search and entitlement probe) to those data sources of the
        KB: the registered source names what the namespace reads, and the graph
        was built from those alone, so a hit from any other data source would
        be text the namespace never registered. Empty means the whole KB.
        """
        if not knowledge_base_id:
            raise ValueError("knowledge_base_id is required")
        if search_mode not in ("vector", "managed"):
            raise ValueError("search_mode must be 'vector' or 'managed'")
        self._knowledge_base_id = knowledge_base_id
        self._region = region or resolve_region()
        self._client = client
        self._client_lock = threading.Lock()
        self._search_mode = search_mode
        self._enforce_user_context = enforce_user_context
        self._data_source_ids = tuple(sorted({str(s) for s in (data_source_ids or ()) if s}))

    @property
    def data_source_ids(self) -> tuple[str, ...]:
        """The data sources every ``Retrieve`` is scoped to (empty = whole KB)."""
        return self._data_source_ids

    def _scope_filter(self) -> dict[str, Any] | None:
        ids = self._data_source_ids
        if not ids:
            return None
        if len(ids) == 1:
            return {"equals": {"key": "_data_source_id", "value": ids[0]}}
        return {"in": {"key": "_data_source_id", "value": list(ids)}}

    def _scoped(self, retrieval_filter: dict[str, Any] | None) -> dict[str, Any] | None:
        """``retrieval_filter`` AND the data-source scope, whichever of the two exist."""
        scope = self._scope_filter()
        if scope is None:
            return retrieval_filter
        if retrieval_filter is None:
            return scope
        return {"andAll": [scope, retrieval_filter]}

    @property
    def enforces_user_context(self) -> bool:
        """True when this KB is queried per user (document ACL applies)."""
        return self._enforce_user_context

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = boto3.client(
                        "bedrock-agent-runtime",
                        region_name=self._region,
                        config=BotoConfig(
                            read_timeout=30,
                            connect_timeout=5,
                            retries={"max_attempts": 2},
                        ),
                    )
        return self._client

    def backend_name(self, namespace: str) -> str:
        """Return the backend label used in traces."""
        return "bedrock-kb"

    def requires_query_embedding(self, namespace: str) -> bool:
        """Bedrock KB embeds natural-language queries internally."""
        return False

    def _user_context(self, user_id: str | None) -> dict[str, str] | None:
        """``userContext`` for a per-user KB, or ``None``; never empty when enforced."""
        if not self._enforce_user_context:
            return None
        caller = (user_id or "").strip() or get_request_user()
        if not caller:
            return None
        return {"userId": caller}

    @instrumented("bedrock-kb")
    async def retrieve(
        self,
        query: str,
        namespace: str,
        *,
        query_embedding: list[float] | None = None,
        top_k: int = DEFAULT_TOP_K,
        retrieval_filter: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> list[ChunkResult]:
        """Retrieve and normalize Bedrock KB results into COA chunk objects.

        ``user_id`` overrides the request-scoped caller; when the KB enforces
        per-user context and neither is known the call returns nothing (the KB
        would otherwise answer with every document the extraction role can see).
        """
        if not query.strip():
            raise ValueError("query is required")

        search_configuration: dict[str, Any] = {
            "numberOfResults": max(1, min(int(top_k), _MAX_TOP_K)),
        }
        # This filter must be resolved server-side from trusted namespace and
        # entitlement policy. It must never be copied directly from a caller.
        # The registered data-source scope is applied on top, always.
        effective_filter = self._scoped(retrieval_filter or None)
        if effective_filter:
            search_configuration["filter"] = effective_filter

        user_context = self._user_context(user_id)
        if self._enforce_user_context and user_context is None:
            logger.warning(
                "bedrock_kb_no_caller_identity", namespace=namespace, knowledge_base_id=self._knowledge_base_id
            )
            return []

        kwargs: dict[str, Any] = {
            "knowledgeBaseId": self._knowledge_base_id,
            "retrievalQuery": {"text": query},
            "retrievalConfiguration": {
                ("managedSearchConfiguration" if self._search_mode == "managed" else "vectorSearchConfiguration"): (
                    search_configuration
                )
            },
        }
        if user_context is not None:
            kwargs["userContext"] = user_context

        client = self._get_client()
        response = await asyncio.to_thread(client.retrieve, **kwargs)

        chunks: list[ChunkResult] = []
        for result in response.get("retrievalResults", []):
            content = result.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if not isinstance(text, str) or not text:
                logger.debug("bedrock_kb_non_text_result_skipped")
                continue

            metadata = result.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            location = _location_uri(result.get("location") or {})
            source_doc = _first_string(metadata, _SOURCE_METADATA_KEYS) or location
            chunk_id = _first_string(metadata, _CHUNK_METADATA_KEYS)
            if chunk_id:
                # The identifier the __Chunk__ node has in this namespace's graph
                # (kg-build emitted aws::kb:<id>; the toolkit stored it under the
                # tenant), so the hit pivots straight into graph traversal.
                chunk_id = stored_chunk_id(to_graphrag_tenant_id(namespace), chunk_id)
            else:
                digest = hashlib.sha256(f"{self._knowledge_base_id}\0{source_doc}\0{text}".encode()).hexdigest()
                chunk_id = f"bedrock-kb:{digest[:24]}"
            label = _first_string(metadata, _LABEL_METADATA_KEYS) or _label_from_uri(source_doc)

            chunks.append(
                ChunkResult(
                    chunk_id=chunk_id,
                    text=text,
                    source_doc=source_doc,
                    relevance_score=float(result.get("score") or 0.0),
                    label=label,
                    uri=location or source_doc,
                )
            )
        return chunks

    async def probe_source_uris(self, source_uris: Iterable[str], user_id: str | None = None) -> dict[str, bool | None]:
        """Per document: ``True`` the caller may read it, ``False`` the KB denies it, ``None`` unknown.

        One ``Retrieve`` per document (``numberOfResults=1``, filtered on
        ``_source_uri``, with ``userContext``): a non-empty answer means the KB
        lets this user see that document; an empty one means it does not. A call
        that fails (throttled, unavailable) yields ``None`` so the caller can
        withhold the document for this request without caching the outage as a
        denial. An unknown caller, or a KB not queried per user, yields ``False``
        for everything (fail closed).
        """
        uris = {u for u in source_uris if u}
        if not uris:
            return {}
        user_context = self._user_context(user_id)
        if user_context is None:
            return dict.fromkeys(uris, False)
        client = self._get_client()
        config_key = "managedSearchConfiguration" if self._search_mode == "managed" else "vectorSearchConfiguration"
        answers: dict[str, bool | None] = {}
        for uri in uris:
            try:
                response = await asyncio.to_thread(
                    client.retrieve,
                    knowledgeBaseId=self._knowledge_base_id,
                    retrievalQuery={"text": "document"},
                    retrievalConfiguration={
                        config_key: {
                            "numberOfResults": 1,
                            "filter": self._scoped({"equals": {"key": "_source_uri", "value": uri}}),
                        }
                    },
                    userContext=user_context,
                )
            except Exception as exc:
                logger.warning("bedrock_kb_entitlement_probe_failed", error=type(exc).__name__)
                answers[uri] = None
                continue
            answers[uri] = bool(response.get("retrievalResults"))
        return answers

    async def entitled_source_uris(self, source_uris: Iterable[str], user_id: str | None = None) -> set[str]:
        """Which of ``source_uris`` the caller may read, per the KB's document ACL.

        Convenience over :meth:`probe_source_uris`: unknown counts as not entitled.
        """
        answers = await self.probe_source_uris(source_uris, user_id=user_id)
        return {uri for uri, ok in answers.items() if ok}

    async def close(self) -> None:
        """Release the lazily-created boto3 client."""
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None


class KbRetrieverCache:
    """One per-user retriever per registered ``BEDROCK_MANAGED_KB`` source, built on first use.

    A source in this deployment's own account is read under the serve runtime's
    own role (the in-account grant in the serve stack); a source carrying a
    ``crossAccountRoleArn`` is read through that role, assumed with the
    namespace's ExternalId exactly as the kg-build task assumes it for
    extraction, so registration and query agree on who may be impersonated.
    Credentials renew themselves for as long as the retriever is cached.

    Keyed on everything the session depends on, so re-registering a source with a
    different role or region builds a new retriever instead of reusing a session
    for the old one. Bounded LRU: evicted retrievers are closed.
    """

    def __init__(
        self,
        *,
        default_region: str,
        max_entries: int = 256,
        session_factory: Callable[[str, str, str, str], boto3.Session] | None = None,
    ) -> None:
        """``session_factory(role_arn, external_id, region, session_name)`` defaults to the refreshable STS assume."""
        self._default_region = default_region
        self._max_entries = max(1, max_entries)
        self._session_factory = session_factory
        self._entries: OrderedDict[tuple[str, str, str, str, tuple[str, ...]], BedrockKnowledgeBaseRetriever] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def get(self, source: Any) -> BedrockKnowledgeBaseRetriever:
        """The retriever for *source* (a ``BedrockKbSource``); the first call may assume the role.

        Synchronous because the assume is: callers on the event loop run it in a
        thread. Raises whatever the assume raises (``DatasourceAssumeError``, or
        ``RuntimeError`` when ``RESOURCE_PREFIX`` is unset and no ExternalId can
        be derived); the admission gate turns either into a fail-closed error.
        """
        region = source.region or self._default_region
        role_arn = source.cross_account_role_arn or ""
        key = (
            source.namespace_id,
            source.knowledge_base_id,
            region,
            role_arn,
            tuple(sorted(source.data_source_ids or ())),
        )
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None:
                self._entries.move_to_end(key)
                return hit
        retriever = self._build(source, region, role_arn)
        with self._lock:
            racing = self._entries.get(key)
            if racing is not None:
                return racing
            self._entries[key] = retriever
            evicted = []
            while len(self._entries) > self._max_entries:
                evicted.append(self._entries.popitem(last=False)[1])
        for old in evicted:
            client = old._client
            if client is not None and callable(getattr(client, "close", None)):
                client.close()
        return retriever

    def _build(self, source: Any, region: str, role_arn: str) -> BedrockKnowledgeBaseRetriever:
        if role_arn:
            from coa_common.constants import datasource_external_id

            from ..clients.datasource_session import refreshable_datasource_session

            factory = self._session_factory or refreshable_datasource_session
            session = factory(
                role_arn,
                datasource_external_id(source.namespace_id),
                region,
                f"coa-serve-{source.namespace_id}",
            )
        else:
            session = boto3.Session(region_name=region)
        client = session.client(
            "bedrock-agent-runtime",
            config=BotoConfig(read_timeout=30, connect_timeout=5, retries={"max_attempts": 2}),
        )
        logger.info(
            "bedrock_kb_retriever_built",
            knowledge_base_id=source.knowledge_base_id,
            namespace=source.namespace_id,
            cross_account=bool(role_arn),
        )
        # A Managed KB carries document-level ACL, so it is always queried per user,
        # and only within the data sources the namespace registered.
        return BedrockKnowledgeBaseRetriever(
            source.knowledge_base_id,
            region=region,
            client=client,
            search_mode="managed",
            enforce_user_context=True,
            data_source_ids=source.data_source_ids,
        )
