# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for managed Bedrock Knowledge Base retrieval."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_serve.tier3.bedrock_kb_retriever import BedrockKnowledgeBaseRetriever
from coa_serve.tier3.document_retriever import RoutingDocumentRetriever
from coa_serve.tier3.vector_retriever import VectorRetriever


@pytest.mark.unit
class TestBedrockKnowledgeBaseRetriever:
    async def test_retrieve_maps_text_score_and_provenance(self):
        client = MagicMock()
        client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "AgentCore provides managed agent runtimes."},
                    "location": {
                        "type": "S3",
                        "s3Location": {"uri": "s3://agentcore-docs/runtime/overview.md"},
                    },
                    "metadata": {"title": "Runtime overview"},
                    "score": 0.87,
                }
            ]
        }
        retriever = BedrockKnowledgeBaseRetriever(
            "ABCDEFGHIJ",
            region="us-east-1",
            client=client,
        )

        chunks = await retriever.retrieve("What is AgentCore?", "managed-docs", top_k=7)

        assert len(chunks) == 1
        assert chunks[0].text == "AgentCore provides managed agent runtimes."
        assert chunks[0].source_doc == "s3://agentcore-docs/runtime/overview.md"
        assert chunks[0].label == "Runtime overview"
        assert chunks[0].relevance_score == 0.87
        assert chunks[0].chunk_id.startswith("bedrock-kb:")
        client.retrieve.assert_called_once_with(
            knowledgeBaseId="ABCDEFGHIJ",
            retrievalQuery={"text": "What is AgentCore?"},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": 7}
            },
        )

    async def test_retrieve_passes_trusted_filter(self):
        client = MagicMock()
        client.retrieve.return_value = {"retrievalResults": []}
        retriever = BedrockKnowledgeBaseRetriever("ABCDEFGHIJ", client=client)
        trusted_filter = {"equals": {"key": "access_group", "value": "managed-docs-readers"}}

        await retriever.retrieve(
            "query",
            "managed-docs",
            retrieval_filter=trusted_filter,
        )

        configuration = client.retrieve.call_args.kwargs["retrievalConfiguration"]
        assert configuration["vectorSearchConfiguration"]["filter"] == trusted_filter

    async def test_retrieve_skips_non_text_results(self):
        client = MagicMock()
        client.retrieve.return_value = {
            "retrievalResults": [
                {"content": {"row": [{"columnName": "x", "columnValue": "y"}]}}
            ]
        }
        retriever = BedrockKnowledgeBaseRetriever("ABCDEFGHIJ", client=client)

        assert await retriever.retrieve("query", "managed-docs") == []

    async def test_rejects_empty_query(self):
        retriever = BedrockKnowledgeBaseRetriever(
            "ABCDEFGHIJ",
            client=MagicMock(),
        )

        with pytest.raises(ValueError, match="query is required"):
            await retriever.retrieve(" ", "managed-docs")


@pytest.mark.unit
class TestRoutingDocumentRetriever:
    async def test_routes_only_configured_namespace_to_kb(self):
        vector_client = MagicMock()
        vector_client.search = MagicMock()
        opensearch = VectorRetriever(vector_client)
        kb = MagicMock()
        kb.retrieve = MagicMock(return_value=[])
        kb.backend_name.return_value = "bedrock-kb"
        kb.requires_query_embedding.return_value = False
        router = RoutingDocumentRetriever(opensearch, {"managed-docs": kb})

        assert router.backend_name("managed-docs") == "bedrock-kb"
        assert router.backend_name("default-search") == "opensearch"
        assert router.requires_query_embedding("managed-docs") is False
        assert router.requires_query_embedding("default-search") is True
