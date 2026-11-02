# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fork (ADR 0015): the agentic vector_search tool on a managed-Knowledge-Base namespace.

When the namespace-aware document retriever reports that a namespace needs no
query embedding (a Bedrock Knowledge Base backs it), the tool retrieves through
it as the signed-in user instead of searching COA's OpenSearch chunk index, and
the agentic retriever prefers it over the graphrag strategy tool for the final
semantic search. OpenSearch-backed namespaces keep the original path.
"""

from __future__ import annotations

import pytest
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.vector_tool import VectorSearchTool, build_vector_tool
from coa_serve.tier3.vector_retriever import ChunkResult


def _chunk(chunk_id: str = "aws:t:kb:abc", text: str = "Decision recorded: Active") -> ChunkResult:
    return ChunkResult(chunk_id=chunk_id, text=text, source_doc="s3://kb/stage-gate-memo-1.txt", relevance_score=0.88)


class FakeVectorRetriever:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, embedding, namespace, *, top_k=10):
        self.calls += 1
        return []


class FakeEmbedClient:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text):
        self.calls += 1
        return [0.1, 0.2]


class FakeDocumentRetriever:
    """Namespace-aware retriever: ``kb-ns`` is a managed KB, everything else OpenSearch."""

    def __init__(self, chunks=None, *, raises: Exception | None = None) -> None:
        self._chunks = list(chunks or [])
        self._raises = raises
        self.calls: list[dict] = []

    def requires_query_embedding(self, namespace: str) -> bool:
        return namespace != "kb-ns"

    def backend_name(self, namespace: str) -> str:
        return "bedrock-kb" if namespace == "kb-ns" else "opensearch"

    async def retrieve(self, query, namespace, *, query_embedding=None, top_k=10, user_id=None):
        self.calls.append(
            {"query": query, "namespace": namespace, "embedding": query_embedding, "top_k": top_k, "user_id": user_id}
        )
        if self._raises is not None:
            raise self._raises
        return list(self._chunks)


@pytest.mark.unit
class TestVectorToolManagedKb:
    @pytest.mark.asyncio
    async def test_kb_namespace_reads_the_kb_as_the_user_without_embedding(self):
        vr, embed, docs = FakeVectorRetriever(), FakeEmbedClient(), FakeDocumentRetriever([_chunk()])
        tool = VectorSearchTool(vr, embed, document_retriever=docs)
        result = await tool.invoke(
            namespace="kb-ns", query="committee decision", top_k=5, profile={"email": "reader@example.com"}
        )
        assert [c.chunk_id for c in result.chunks] == ["aws:t:kb:abc"]
        assert result.items[0]["chunk_id"] == "aws:t:kb:abc"
        assert "via bedrock-kb" in result.detail
        assert docs.calls == [
            {
                "query": "committee decision",
                "namespace": "kb-ns",
                "embedding": None,
                "top_k": 5,
                "user_id": "reader@example.com",
            }
        ]
        assert vr.calls == 0 and embed.calls == 0

    @pytest.mark.asyncio
    async def test_opensearch_namespace_keeps_the_original_path(self):
        vr, embed, docs = FakeVectorRetriever(), FakeEmbedClient(), FakeDocumentRetriever([_chunk()])
        tool = VectorSearchTool(vr, embed, document_retriever=docs)
        await tool.invoke(namespace="oss-ns", query="q")
        assert docs.calls == [] and embed.calls == 1 and vr.calls == 1

    @pytest.mark.asyncio
    async def test_kb_error_degrades_without_raising(self):
        docs = FakeDocumentRetriever(raises=RuntimeError("AccessDenied"))
        tool = VectorSearchTool(FakeVectorRetriever(), FakeEmbedClient(), document_retriever=docs)
        result = await tool.invoke(namespace="kb-ns", query="q")
        assert result.chunks == () and "RuntimeError" in result.detail

    @pytest.mark.asyncio
    async def test_kb_without_query_text_returns_degraded_result(self):
        tool = VectorSearchTool(
            FakeVectorRetriever(), FakeEmbedClient(), document_retriever=FakeDocumentRetriever([_chunk()])
        )
        result = await tool.invoke(namespace="kb-ns", embedding=[0.1, 0.2])
        assert result.chunks == () and "no query text" in result.detail

    def test_handles_namespace_only_for_kb_backed_namespaces(self):
        tool = VectorSearchTool(FakeVectorRetriever(), FakeEmbedClient(), document_retriever=FakeDocumentRetriever())
        assert tool.handles_namespace("kb-ns") is True
        assert tool.handles_namespace("oss-ns") is False
        assert VectorSearchTool(FakeVectorRetriever(), FakeEmbedClient()).handles_namespace("kb-ns") is False

    def test_builder_passes_the_document_retriever(self):
        registry = ToolRegistry()
        docs = FakeDocumentRetriever()
        name = build_vector_tool(registry, FakeVectorRetriever(), FakeEmbedClient(), document_retriever=docs)
        assert registry.get(name).handles_namespace("kb-ns") is True
