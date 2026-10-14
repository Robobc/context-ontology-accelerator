from __future__ import annotations

import pytest
from coa_outer_agent.evidence import Evidence
from coa_outer_agent.identity import TrustedIdentity
from coa_outer_agent.mcp_client import McpToolError
from coa_outer_agent.orchestrator import OuterAgent, UnresolvedSemanticPlanError
from coa_outer_agent.source_adapters import SourceExecutionError

pytestmark = pytest.mark.unit


class FakeGenerator:
    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return "Grounded answer [E1]"


class FakePlanFactory:
    def __init__(self, *, status="RESOLVED", plan_error=False):
        self.calls = []
        self.status = status
        self.plan_error = plan_error

    def __call__(self, url, token):
        factory = self

        class Client:
            async def call_tool(self, name, arguments):
                factory.calls.append((url, token, name, arguments))
                if factory.plan_error:
                    raise McpToolError("planning unavailable")
                directives = (
                    [
                        {
                            "targetId": "domain-kb",
                            "targetKind": "MANAGED_KB",
                            "language": "NATURAL_LANGUAGE",
                            "query": "Which document? Resolved: GovernanceDocument",
                            "readOnly": True,
                            "requiresCompilation": False,
                            "expectedEvidence": ["DOCUMENT", "CITATION", "PROVENANCE"],
                        }
                    ]
                    if factory.status == "RESOLVED"
                    else []
                )
                return {
                    "plan": {
                        "planVersion": "1.0",
                        "status": factory.status,
                        "namespaceId": "123e4567-e89b-42d3-a456-426614174000",
                        "profileMode": "ADOPT",
                        "profileVersion": "domain-v1",
                        "originalQuestion": "Which document?",
                        "groundedQuestion": "Which document? Resolved: GovernanceDocument",
                        "ontologyVersion": "ontology-2026-10-14",
                        "resolvedConcepts": [],
                        "canonicalEntities": [],
                        "ambiguities": (
                            [{"code": "AMBIGUOUS", "message": "clarify", "candidates": []}]
                            if factory.status != "RESOLVED"
                            else []
                        ),
                        "sourceDirectives": directives,
                        "expectedEvidence": ["DOCUMENT", "CITATION", "PROVENANCE"] if directives else [],
                        "generationTarget": "portkey",
                    }
                }

        return Client()


class FakeSourceAdapters:
    def __init__(self, *, fail=False, empty=False):
        self.calls = []
        self.fail = fail
        self.empty = empty

    async def execute(self, directive, identity):
        self.calls.append((directive, identity))
        if self.fail:
            raise SourceExecutionError("source unavailable")
        if self.empty:
            return []
        return [
            Evidence(
                evidence_id="E1",
                target_id=directive.targetId,
                target_kind=directive.targetKind,
                text="evidence",
                source="sharepoint://document",
                score=0.9,
                source_retriever="kb",
                metadata={},
            )
        ]


def identity():
    return TrustedIdentity(
        bearer_token="validated-token",
        subject="sub",
        user_id="person@example.com",
    )


@pytest.mark.asyncio
async def test_composes_plan_native_evidence_and_generation():
    generator = FakeGenerator()
    plans = FakePlanFactory()
    sources = FakeSourceAdapters()
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=sources,
        generator=generator,
        mcp_client_factory=plans,
    )

    result = await agent.resolve(question="Which document?", namespace="project", identity=identity())

    assert result["answer"] == "Grounded answer [E1]"
    assert result["semanticPlan"]["ontologyVersion"] == "ontology-2026-10-14"
    assert result["evidence"][0]["target_id"] == "domain-kb"
    assert plans.calls[0][2] == "semantic_execution_plan"
    assert sources.calls[0][1].user_id == "person@example.com"
    assert generator.calls[0]["context"].startswith("[E1] target=domain-kb kind=MANAGED_KB")


@pytest.mark.asyncio
async def test_ambiguous_plan_never_executes_sources_or_generation():
    generator = FakeGenerator()
    sources = FakeSourceAdapters()
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=sources,
        generator=generator,
        mcp_client_factory=FakePlanFactory(status="AMBIGUOUS"),
    )
    with pytest.raises(UnresolvedSemanticPlanError, match="AMBIGUOUS"):
        await agent.resolve(question="Question", namespace="project", identity=identity())
    assert sources.calls == []
    assert generator.calls == []


@pytest.mark.asyncio
async def test_invalid_plan_fails_closed():
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=FakeSourceAdapters(),
        generator=FakeGenerator(),
        mcp_client_factory=FakePlanFactory(plan_error=True),
    )
    with pytest.raises(UnresolvedSemanticPlanError, match="valid semantic"):
        await agent.resolve(question="Question", namespace="project", identity=identity())


@pytest.mark.asyncio
async def test_source_failure_never_generates_from_partial_context():
    generator = FakeGenerator()
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=FakeSourceAdapters(fail=True),
        generator=generator,
        mcp_client_factory=FakePlanFactory(),
    )
    with pytest.raises(SourceExecutionError):
        await agent.resolve(question="Question", namespace="project", identity=identity())
    assert generator.calls == []


@pytest.mark.asyncio
async def test_empty_authorized_evidence_returns_deterministic_refusal():
    generator = FakeGenerator()
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=FakeSourceAdapters(empty=True),
        generator=generator,
        mcp_client_factory=FakePlanFactory(),
    )
    result = await agent.resolve(question="Question", namespace="project", identity=identity())
    assert result["answer"].startswith("Insufficient authorized evidence")
    assert generator.calls == []
