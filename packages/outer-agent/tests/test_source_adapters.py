# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_outer_agent import Evidence
from coa_outer_agent.identity import TrustedIdentity
from coa_outer_agent.plan_models import SourceDirective
from coa_outer_agent.source_adapters import (
    SourceAdapterRegistry,
    SourceExecutionError,
    SourceTargetConfig,
)

pytestmark = pytest.mark.unit


def _identity() -> TrustedIdentity:
    return TrustedIdentity("token", "subject", "person@example.com")


def _directive(kind="MANAGED_KB", target_id="domain-kb") -> SourceDirective:
    return SourceDirective(
        targetId=target_id,
        targetKind=kind,
        language="NATURAL_LANGUAGE" if kind == "MANAGED_KB" else "SEMANTIC_INTENT",
        query="find project P-123",
        readOnly=True,
        requiresCompilation=kind != "MANAGED_KB",
        expectedEvidence=["DOCUMENT", "PROVENANCE"],
    )


@pytest.mark.asyncio
async def test_managed_kb_uses_trusted_identity_and_disables_generation():
    stream = iter(
        [
            {
                "result": {
                    "results": [
                        {
                            "content": {"text": "approved evidence"},
                            "metadata": {"sourceUri": "sharepoint://doc"},
                        }
                    ]
                }
            }
        ]
    )
    client = MagicMock()
    client.agentic_retrieve_stream.return_value = {"stream": stream}
    registry = SourceAdapterRegistry(
        region="us-east-1",
        targets={"domain-kb": SourceTargetConfig("domain-kb", "MANAGED_KB", ("ABCDEFGHIJ",))},
        managed_kb_client=client,
    )

    evidence = await registry.execute(_directive(), _identity())

    request = client.agentic_retrieve_stream.call_args.kwargs
    assert request["generateResponse"] is False
    assert request["userContext"] == {"userId": "person@example.com"}
    assert request["retrievers"][0]["configuration"]["knowledgeBase"]["knowledgeBaseId"] == "ABCDEFGHIJ"
    assert evidence[0].target_id == "domain-kb"
    assert evidence[0].source == "sharepoint://doc"


@pytest.mark.asyncio
async def test_unconfigured_target_fails_closed():
    registry = SourceAdapterRegistry(region="us-east-1", targets={})
    with pytest.raises(SourceExecutionError, match="not configured"):
        await registry.execute(_directive(), _identity())


@pytest.mark.asyncio
async def test_unknown_obo_contract_is_not_invented():
    registry = SourceAdapterRegistry(
        region="us-east-1",
        targets={"analytics-query": SourceTargetConfig("analytics-query", "GOVERNED_QUERY")},
    )
    with pytest.raises(SourceExecutionError, match="No governed adapter"):
        await registry.execute(_directive("GOVERNED_QUERY", "analytics-query"), _identity())


@pytest.mark.asyncio
async def test_generic_adapter_plugin_executes_without_core_changes():
    adapter = AsyncMock()
    adapter.execute.return_value = []
    target = SourceTargetConfig("analytics-query", "GOVERNED_QUERY")
    registry = SourceAdapterRegistry(
        region="us-east-1",
        targets={"analytics-query": target},
        adapters={"GOVERNED_QUERY": adapter},
    )
    directive = _directive("GOVERNED_QUERY", "analytics-query")

    assert await registry.execute(directive, _identity()) == []
    adapter.execute.assert_awaited_once_with(directive, target, _identity())


@pytest.mark.asyncio
async def test_generated_response_in_evidence_mode_is_rejected():
    client = MagicMock()
    client.agentic_retrieve_stream.return_value = {
        "stream": iter(
            [
                {
                    "result": {
                        "results": [],
                        "generatedResponse": {"answer": "unexpected"},
                    }
                }
            ]
        )
    }
    registry = SourceAdapterRegistry(
        region="us-east-1",
        targets={"domain-kb": SourceTargetConfig("domain-kb", "MANAGED_KB", ("ABCDEFGHIJ",))},
        managed_kb_client=client,
    )
    with pytest.raises(SourceExecutionError, match="despite evidence-only"):
        await registry.execute(_directive(), _identity())


@pytest.mark.asyncio
async def test_target_kind_mismatch_fails_before_plugin_execution():
    adapter = AsyncMock()
    registry = SourceAdapterRegistry(
        region="us-east-1",
        targets={"analytics-query": SourceTargetConfig("analytics-query", "GOVERNED_QUERY")},
        adapters={"EXTERNAL_GRAPH": adapter},
    )

    with pytest.raises(SourceExecutionError, match="kind mismatch"):
        await registry.execute(_directive("EXTERNAL_GRAPH", "analytics-query"), _identity())
    adapter.execute.assert_not_awaited()


def test_reserved_managed_kb_adapter_cannot_be_overridden():
    with pytest.raises(ValueError, match="reserved"):
        SourceAdapterRegistry(
            region="us-east-1",
            targets={},
            adapters={"MANAGED_KB": AsyncMock()},
        )


@pytest.mark.asyncio
async def test_plugin_output_must_use_typed_evidence():
    adapter = AsyncMock()
    adapter.execute.return_value = [object()]
    registry = SourceAdapterRegistry(
        region="us-east-1",
        targets={"analytics-query": SourceTargetConfig("analytics-query", "GOVERNED_QUERY")},
        adapters={"GOVERNED_QUERY": adapter},
    )

    with pytest.raises(SourceExecutionError, match="invalid evidence"):
        await registry.execute(_directive("GOVERNED_QUERY", "analytics-query"), _identity())


@pytest.mark.asyncio
async def test_plugin_output_must_preserve_target_provenance():
    adapter = AsyncMock()
    adapter.execute.return_value = [Evidence("E1", "other-target", "GOVERNED_QUERY", "row", "source", None, None, {})]
    registry = SourceAdapterRegistry(
        region="us-east-1",
        targets={"analytics-query": SourceTargetConfig("analytics-query", "GOVERNED_QUERY")},
        adapters={"GOVERNED_QUERY": adapter},
    )

    with pytest.raises(SourceExecutionError, match="mismatched evidence provenance"):
        await registry.execute(_directive("GOVERNED_QUERY", "analytics-query"), _identity())
