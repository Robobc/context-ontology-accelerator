# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Document-retrieval contract and namespace router.

Document retrieval is intentionally separate from the ontology vector index.
OpenSearch can implement both concerns, but a managed Bedrock Knowledge Base
accepts natural-language queries and cannot store COA's class/property vectors.
Keeping the contracts separate lets a namespace use a managed KB for passages
while the ontology engine continues to use OpenSearch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from .vector_retriever import DEFAULT_TOP_K, ChunkResult


@runtime_checkable
class DocumentRetriever(Protocol):
    """Retrieve document passages for answer grounding."""

    async def retrieve(
        self,
        query: str,
        namespace: str,
        *,
        query_embedding: list[float] | None = None,
        top_k: int = DEFAULT_TOP_K,
        retrieval_filter: dict[str, Any] | None = None,
    ) -> list[ChunkResult]:
        """Return ranked passages for ``query`` within ``namespace``."""
        ...

    def backend_name(self, namespace: str) -> str:
        """Return the backend selected for ``namespace`` for traces/metrics."""
        ...

    def requires_query_embedding(self, namespace: str) -> bool:
        """Whether retrieval requires a caller-produced query embedding."""
        ...


class RoutingDocumentRetriever:
    """Route document retrieval by namespace without changing the public API."""

    def __init__(
        self,
        default: DocumentRetriever,
        routes: Mapping[str, DocumentRetriever] | None = None,
    ) -> None:
        """Configure a fallback and optional exact namespace overrides."""
        self._default = default
        self._routes = dict(routes or {})

    def _resolve(self, namespace: str) -> DocumentRetriever:
        return self._routes.get(namespace, self._default)

    async def retrieve(
        self,
        query: str,
        namespace: str,
        *,
        query_embedding: list[float] | None = None,
        top_k: int = DEFAULT_TOP_K,
        retrieval_filter: dict[str, Any] | None = None,
    ) -> list[ChunkResult]:
        """Delegate passage retrieval to the namespace-selected backend."""
        retriever = self._resolve(namespace)
        return await retriever.retrieve(
            query,
            namespace,
            query_embedding=query_embedding,
            top_k=top_k,
            retrieval_filter=retrieval_filter,
        )

    def backend_name(self, namespace: str) -> str:
        """Return the selected backend's trace label."""
        return self._resolve(namespace).backend_name(namespace)

    def requires_query_embedding(self, namespace: str) -> bool:
        """Return the selected backend's embedding requirement."""
        return self._resolve(namespace).requires_query_embedding(namespace)
