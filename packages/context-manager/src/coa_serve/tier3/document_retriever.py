# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Document-retrieval contract and per-request source router (fork).

Document retrieval is intentionally separate from the ontology vector index.
OpenSearch can implement both concerns, but a managed Bedrock Knowledge Base
accepts natural-language queries and cannot store COA's class/property vectors.
Keeping the contracts separate lets a namespace use a managed KB for passages
while the ontology engine continues to use OpenSearch.
"""

from __future__ import annotations

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
        user_id: str | None = None,
    ) -> list[ChunkResult]:
        """Return ranked passages for ``query`` within ``namespace``.

        ``user_id`` names the caller for backends that answer per user (Bedrock
        KB document ACL); backends without per-user views ignore it.
        """
        ...

    def backend_name(self, namespace: str) -> str:
        """Return the backend selected for ``namespace`` for traces/metrics."""
        ...

    def requires_query_embedding(self, namespace: str) -> bool:
        """Whether retrieval requires a caller-produced query embedding."""
        ...


class RegistryDocumentRetriever:
    """Route document retrieval to the namespace's registered Knowledge Base, else the default.

    The Knowledge Base is a source like any other: which one a namespace reads
    from is the ``BEDROCK_MANAGED_KB`` record in its source registry, resolved by
    the admission gate and bound to the request
    (:mod:`coa_serve.request_document_source`). This class only reads that
    binding; there is no deployment-level namespace-to-KB map. A request with no
    binding (a namespace without a Knowledge Base source) uses *default*, COA's
    own OpenSearch chunk index.
    """

    def __init__(self, default: DocumentRetriever) -> None:
        """``default`` serves every namespace that has no Knowledge Base source."""
        self._default = default

    @staticmethod
    def bound_kb_retriever(namespace: str) -> Any | None:
        """The per-user Knowledge Base retriever bound to this request for *namespace*, or ``None``."""
        from ..request_document_source import get_request_document_source

        bound = get_request_document_source()
        if bound is None or bound.namespace != namespace:
            return None
        return bound.retriever

    def _resolve(self, namespace: str) -> DocumentRetriever:
        return self.bound_kb_retriever(namespace) or self._default

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
        """Delegate passage retrieval to the namespace-selected backend."""
        retriever = self._resolve(namespace)
        return await retriever.retrieve(
            query,
            namespace,
            query_embedding=query_embedding,
            top_k=top_k,
            retrieval_filter=retrieval_filter,
            user_id=user_id,
        )

    def backend_name(self, namespace: str) -> str:
        """Return the selected backend's trace label."""
        return self._resolve(namespace).backend_name(namespace)

    def requires_query_embedding(self, namespace: str) -> bool:
        """Return the selected backend's embedding requirement."""
        return self._resolve(namespace).requires_query_embedding(namespace)
