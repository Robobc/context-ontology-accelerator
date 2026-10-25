# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Document-level entitlement for graph-derived text (fork, Bedrock Managed KB).

When a namespace's documents live in a Bedrock Knowledge Base with document-level
ACL, ``Retrieve`` already answers per user. The lexical graph does not: it was
extracted under the extraction role and holds every document's chunks, facts and
statements, so a graph hop (adjacent chunks, an entity's supporting statements)
would show a user text from a document the KB would never have returned to them.

This gate closes that hop. Every piece of graph text has provenance to a
``__Source__`` node (``aws:doc:<documentId>``, property ``source_uri``). Before
text is returned, the gate resolves the distinct source documents behind it with
one openCypher query and asks the KB, per document, whether *this* caller may
read it (``BedrockKnowledgeBaseRetriever.entitled_source_uris``: one filtered
``Retrieve`` with ``userContext``). Text whose document is not entitled, or
whose provenance cannot be resolved, is dropped (fail closed).

Scope: active only for namespaces whose document retriever is a per-user Bedrock
KB (``enforces_user_context``); everything else passes through untouched. Only
KB-sourced chunks (ids prefixed ``aws:``) are subject to the chunk check; a
chunk the toolkit minted itself did not come from the KB.

ponytail: entitlement answers are cached per (user, namespace, uri) for
``cache_ttl_s`` (default 60 s), unbounded within that window. Fine for the
hundreds of documents one agent turn touches; swap for an LRU if a namespace
ever fans out to tens of thousands.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog

from ..request_identity import get_request_user
from .vector_retriever import ChunkResult

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coa-entitlement")
KB_CHUNK_ID_PREFIX = "aws:"

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
STATEMENT_PROVENANCE: str = (
    "MATCH (e:{entity_label})-[:`__SUBJECT__`|`__OBJECT__`]->()-[:`__SUPPORTS__`]->(st:{statement_label})\n"
    "WHERE id(e) IN $entity_ids\n"
    "MATCH (st)-[:`__MENTIONED_IN__`]->(c:{chunk_label})-[:`__EXTRACTED_FROM__`]->(s:{source_label})\n"
    "RETURN id(e) AS entityId, st.value AS text, s.source_uri AS uri"
)


class DocumentEntitlementGate:
    """Filter graph-derived ``ChunkResult``s down to documents the caller may read."""

    def __init__(
        self,
        graph_store_uri: str,
        kb_retriever_for: Callable[[str], Any | None],
        *,
        query_timeout_s: float = 15.0,
        cache_ttl_s: float = 60.0,
    ) -> None:
        """``kb_retriever_for(namespace)`` returns that namespace's Bedrock KB retriever or ``None``."""
        self._graph_store_uri = graph_store_uri
        self._kb_retriever_for = kb_retriever_for
        self._query_timeout_s = query_timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._store: Any = None
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

    async def _run_cypher(self, cypher: str, params: dict) -> list[dict]:
        store = self._get_store()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_EXECUTOR, lambda: store.execute_query_with_retry(cypher, params))
        rows = await asyncio.wait_for(asyncio.shield(fut), timeout=self._query_timeout_s)
        return list(rows or [])

    # ── entitlement ─────────────────────────────────────────────────────

    async def entitled_uris(self, namespace: str, uris: Iterable[str], user_id: str | None = None) -> set[str]:
        """Subset of ``uris`` the caller may read; cached per (user, namespace, uri)."""
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
            fresh = await retriever.entitled_source_uris(to_ask, user_id=caller)
            expiry = now + self._cache_ttl_s
            for uri in to_ask:
                ok = uri in fresh
                self._cache[(caller, namespace, uri)] = (ok, expiry)
                if ok:
                    entitled.add(uri)
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
        if not kb_ids:
            return chunks
        rows = await self._run_cypher(
            CHUNK_PROVENANCE.format(
                chunk_label=self._label(namespace, "__Chunk__"), source_label=self._label(namespace, "__Source__")
            ),
            {"chunk_ids": kb_ids},
        )
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
            STATEMENT_PROVENANCE.format(
                entity_label=self._label(namespace, "__Entity__"),
                statement_label=self._label(namespace, "__Statement__"),
                chunk_label=self._label(namespace, "__Chunk__"),
                source_label=self._label(namespace, "__Source__"),
            ),
            {"entity_ids": entity_ids},
        )
        uris_for: dict[tuple[str, str], set[str]] = {}
        for r in rows:
            uris_for.setdefault((str(r.get("entityId")), str(r.get("text") or "")), set()).add(str(r.get("uri") or ""))
        allowed = await self.entitled_uris(namespace, (u for s in uris_for.values() for u in s), user_id)
        kept = [c for c in chunks if uris_for.get((c.uri, c.text), set()) & allowed]
        self._log_dropped(namespace, "statements", len(chunks), len(kept))
        return kept

    @staticmethod
    def _log_dropped(namespace: str, kind: str, before: int, after: int) -> None:
        if after < before:
            logger.info("document_entitlement_filtered", namespace=namespace, kind=kind, before=before, after=after)


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
