# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Amazon Bedrock Knowledge Bases document retriever."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import boto3
import structlog
from botocore.config import Config as BotoConfig
from coa_common import resolve_region

from ..clients.base import instrumented
from .vector_retriever import _MAX_TOP_K, DEFAULT_TOP_K, ChunkResult

logger = structlog.get_logger(__name__)

_SOURCE_METADATA_KEYS = (
    "x-amz-bedrock-kb-source-uri",
    "source_uri",
    "source",
    "document_id",
)
_CHUNK_METADATA_KEYS = (
    "x-amz-bedrock-kb-chunk-id",
    "chunk_id",
)
_LABEL_METADATA_KEYS = ("title", "document_title", "name")


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
    ) -> None:
        """Configure one existing Knowledge Base and an optional test client."""
        if not knowledge_base_id:
            raise ValueError("knowledge_base_id is required")
        self._knowledge_base_id = knowledge_base_id
        self._region = region or resolve_region()
        self._client = client
        self._client_lock = threading.Lock()

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

    @instrumented("bedrock-kb")
    async def retrieve(
        self,
        query: str,
        namespace: str,
        *,
        query_embedding: list[float] | None = None,
        top_k: int = DEFAULT_TOP_K,
        retrieval_filter: dict[str, Any] | None = None,
    ) -> list[ChunkResult]:
        """Retrieve and normalize Bedrock KB results into COA chunk objects."""
        if not query.strip():
            raise ValueError("query is required")

        vector_configuration: dict[str, Any] = {
            "numberOfResults": max(1, min(int(top_k), _MAX_TOP_K)),
        }
        if retrieval_filter:
            # This filter must be resolved server-side from trusted namespace and
            # entitlement policy. It must never be copied directly from a caller.
            vector_configuration["filter"] = retrieval_filter

        client = self._get_client()
        response = await asyncio.to_thread(
            client.retrieve,
            knowledgeBaseId=self._knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vector_configuration},
        )

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
            if not chunk_id:
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

    async def close(self) -> None:
        """Release the lazily-created boto3 client."""
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None
