# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Amazon Bedrock Knowledge Bases document retriever."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import boto3
import structlog
from botocore.config import Config as BotoConfig
from coa_common import resolve_region

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
# own ids under this prefix (the toolkit keeps 'aws:' ids verbatim), so a
# Retrieve hit and its __Chunk__ node share one identifier.
KB_CHUNK_ID_PREFIX = "aws:"


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
    ) -> None:
        """Configure one existing Knowledge Base and an optional test client.

        ``search_mode`` is ``"vector"`` (``vectorSearchConfiguration``, the
        default and today's behaviour) or ``"managed"`` (Bedrock Managed Knowledge
        Bases, ``managedSearchConfiguration``). With ``enforce_user_context`` every
        ``Retrieve`` carries ``userContext.userId`` for the request's caller so the
        KB applies its document-level ACL; a request with no caller then returns
        no chunks rather than everyone's.
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
        if retrieval_filter:
            # This filter must be resolved server-side from trusted namespace and
            # entitlement policy. It must never be copied directly from a caller.
            search_configuration["filter"] = retrieval_filter

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
                # Same identifier the kg-build loader gave the __Chunk__ node, so
                # the hit pivots straight into graph traversal.
                chunk_id = f"{KB_CHUNK_ID_PREFIX}{chunk_id}"
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

    async def entitled_source_uris(self, source_uris: Iterable[str], user_id: str | None = None) -> set[str]:
        """Which of ``source_uris`` the caller may read, per the KB's document ACL.

        One ``Retrieve`` per document (``numberOfResults=1``, filtered on
        ``_source_uri``, with ``userContext``): a non-empty answer means the KB
        lets this user see that document. Used by the document entitlement gate
        before graph-derived text (adjacent chunks, supporting statements) is
        shown. Fails closed: an unknown caller or a KB error entitles nothing.
        """
        uris = {u for u in source_uris if u}
        if not uris:
            return set()
        user_context = self._user_context(user_id)
        if user_context is None:
            return set()
        client = self._get_client()
        config_key = "managedSearchConfiguration" if self._search_mode == "managed" else "vectorSearchConfiguration"
        entitled: set[str] = set()
        for uri in uris:
            try:
                response = await asyncio.to_thread(
                    client.retrieve,
                    knowledgeBaseId=self._knowledge_base_id,
                    retrievalQuery={"text": "document"},
                    retrievalConfiguration={
                        config_key: {
                            "numberOfResults": 1,
                            "filter": {"equals": {"key": "_source_uri", "value": uri}},
                        }
                    },
                    userContext=user_context,
                )
            except Exception as exc:
                logger.warning("bedrock_kb_entitlement_probe_failed", error=type(exc).__name__)
                continue
            if response.get("retrievalResults"):
                entitled.add(uri)
        return entitled

    async def close(self) -> None:
        """Release the lazily-created boto3 client."""
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None
