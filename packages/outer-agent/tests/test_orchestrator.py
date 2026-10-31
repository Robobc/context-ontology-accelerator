from __future__ import annotations

import json

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


EXPANSION = {
    "entities": [{"id": "e:atlas", "label": "Project Atlas", "class": "Project"}],
    "facts": [
        {
            "id": "f1",
            "text": "Project Atlas HAS BUDGET 1.2M EUR",
            "subjectId": "e:atlas",
            "chunkId": "Ukhx_fin",
            "sourceDocumentId": "sharepoint://document",
        }
    ],
    "relatedChunks": [{"chunkId": "Ukhx_hr", "sourceDocumentId": "sharepoint://hr", "entityIds": ["e:atlas"]}],
    "withheld": {"seedChunks": 0, "facts": 1, "relatedChunks": 0},
    "trace": [],
}


class FakePlanFactory:
    def __init__(self, *, status="RESOLVED", plan_error=False, expand_error=False, expansion=None):
        self.calls = []
        self.status = status
        self.plan_error = plan_error
        self.expand_error = expand_error
        self.expansion = EXPANSION if expansion is None else expansion

    def __call__(self, url, token):
        factory = self

        class Client:
            async def call_tool(self, name, arguments):
                factory.calls.append((url, token, name, arguments))
                if name == "expand_chunks":
                    if factory.expand_error:
                        raise McpToolError("graph unavailable")
                    # The MCP tool returns its JSON serialized, as the real server does.
                    return {"content": [{"type": "text", "text": json.dumps(factory.expansion)}]}
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
    def __init__(self, *, fail=False, empty=False, chunk_id="Ukhx_fin"):
        self.calls = []
        self.fail = fail
        self.empty = empty
        self.chunk_id = chunk_id

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
                metadata={"_chunk_id": self.chunk_id} if self.chunk_id else {},
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


# ── ADR 0012: Knowledge Base hits pivot into COA's graph ─────────────────────


@pytest.mark.asyncio
async def test_kb_hits_are_expanded_through_coa_with_the_users_token_and_cited():
    generator = FakeGenerator()
    plans = FakePlanFactory()
    agent = OuterAgent(
        coa_mcp_url="https://coa", source_adapters=FakeSourceAdapters(), generator=generator, mcp_client_factory=plans
    )

    result = await agent.resolve(question="Budget?", namespace="project", identity=identity())

    url, token, name, arguments = plans.calls[1]
    assert (url, token, name) == ("https://coa", "validated-token", "expand_chunks")
    assert arguments == {
        "chunkIds": ["Ukhx_fin"],
        "namespaceId": "project",
        "scope": "SEED_DOCUMENTS",
        "maxFacts": 100,
    }
    kinds = [item["target_kind"] for item in result["evidence"]]
    assert kinds == ["MANAGED_KB", "COA_GRAPH"]
    assert [item["evidence_id"] for item in result["evidence"]] == ["E1", "E2"]  # one numbering across both legs
    assert (
        "[E2] target=coa-graph kind=COA_GRAPH source=sharepoint://document\nProject Atlas HAS BUDGET"
        in (generator.calls[0]["context"])
    )
    expand_step = next(step for step in result["trace"] if step["step"] == "coa.expand_chunks")
    assert expand_step["status"] == "resolved"
    assert expand_step["detail"] == {
        "seeds": 1,
        "scope": "SEED_DOCUMENTS",
        "facts": 1,
        "relatedChunks": 1,
        "withheld": {"seedChunks": 0, "facts": 1, "relatedChunks": 0},
    }


@pytest.mark.asyncio
async def test_expansion_scope_and_fact_budget_are_configurable():
    plans = FakePlanFactory()
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=FakeSourceAdapters(),
        generator=FakeGenerator(),
        mcp_client_factory=plans,
        graph_expansion_scope="ENTITLED_DOCUMENTS",
        graph_expansion_max_facts=25,
    )
    await agent.resolve(question="Budget?", namespace="project", identity=identity())
    assert plans.calls[1][3]["scope"] == "ENTITLED_DOCUMENTS"
    assert plans.calls[1][3]["maxFacts"] == 25


def test_unknown_expansion_scope_is_rejected_at_construction():
    with pytest.raises(ValueError, match="graph_expansion_scope"):
        OuterAgent(
            coa_mcp_url="https://coa",
            source_adapters=FakeSourceAdapters(),
            generator=FakeGenerator(),
            graph_expansion_scope="EVERYTHING",
        )


@pytest.mark.asyncio
async def test_expansion_off_never_calls_coa_twice():
    plans = FakePlanFactory()
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=FakeSourceAdapters(),
        generator=FakeGenerator(),
        mcp_client_factory=plans,
        graph_expansion_scope=None,
    )
    result = await agent.resolve(question="Budget?", namespace="project", identity=identity())
    assert [call[2] for call in plans.calls] == ["semantic_execution_plan"]
    assert [item["target_kind"] for item in result["evidence"]] == ["MANAGED_KB"]


@pytest.mark.asyncio
async def test_hits_without_chunk_ids_are_not_expanded():
    plans = FakePlanFactory()
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=FakeSourceAdapters(chunk_id=None),
        generator=FakeGenerator(),
        mcp_client_factory=plans,
    )
    await agent.resolve(question="Budget?", namespace="project", identity=identity())
    assert [call[2] for call in plans.calls] == ["semantic_execution_plan"]


@pytest.mark.asyncio
async def test_unavailable_graph_degrades_to_source_evidence_with_a_trace_entry():
    """Enrichment, not a directive: the Knowledge Base evidence still answers, and the trace says why alone."""
    generator = FakeGenerator()
    plans = FakePlanFactory(expand_error=True)
    agent = OuterAgent(
        coa_mcp_url="https://coa", source_adapters=FakeSourceAdapters(), generator=generator, mcp_client_factory=plans
    )
    result = await agent.resolve(question="Budget?", namespace="project", identity=identity())
    assert result["answer"] == "Grounded answer [E1]"
    assert [item["target_kind"] for item in result["evidence"]] == ["MANAGED_KB"]
    expand_step = next(step for step in result["trace"] if step["step"] == "coa.expand_chunks")
    assert expand_step["status"] == "unavailable"
    assert generator.calls, "generation still runs from the authorized source evidence"


@pytest.mark.asyncio
async def test_empty_expansion_adds_nothing():
    plans = FakePlanFactory(expansion={"entities": [], "facts": [], "relatedChunks": [], "withheld": {}})
    agent = OuterAgent(
        coa_mcp_url="https://coa",
        source_adapters=FakeSourceAdapters(),
        generator=FakeGenerator(),
        mcp_client_factory=plans,
    )
    result = await agent.resolve(question="Budget?", namespace="project", identity=identity())
    assert [item["target_kind"] for item in result["evidence"]] == ["MANAGED_KB"]


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
