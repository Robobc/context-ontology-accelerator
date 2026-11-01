# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deep-reasoning planner profile (ADR 0013).

The profile is an opt-in, per-request switch (``options.plannerProfile``, deployment
default ``DEEP_REASONING_PLANNER_PROFILE``) with two values:

* ``legacy`` — the graph-first standing strategy, byte for byte the prompt that
  shipped before the switch existed. Everything in this file that runs under
  ``legacy`` asserts that NOTHING observable changed.
* ``structured_first`` — (A) the planner prompt decides a sub-question's modality
  first and sends tabular sub-questions straight to ``nl_to_sql`` /
  ``nl_to_sparql``; (B) the retriever resolves the namespace ontology ONCE before
  the sub-questions fan out, so no fork spends a reasoning step on its own
  ``ontology_lookup``.

Observed failure that motivates it (dev, 2026-11-01): a mixed question decomposed
into a tabular and a document sub-question; under a shared 5-step budget both
forks spent their first steps on ``ontology_lookup`` + graph anchoring and
``nl_to_sql`` was never selected (``max_steps_reached``). The regression pair at
the bottom reproduces that shape with a scripted planner: same tools, same
budget, legacy exhausts the steps, ``structured_first`` does not.

Covered here: ``propose_system_for`` and the prompt the Bedrock planner sends per
profile; ``AccumulatedContext.fork`` carrying the profile; the retriever's profile
resolution, the pre-resolve step (success / absent tool / withheld tool / failure /
no ontology in result) and its trace rows; the request-model validation of
``options.plannerProfile``; ``ServiceConfig`` env parsing; the factory wiring; and
the lazy-import invariant for the new planner constants.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.models import PlannerDecision, StopReason, SubQuestion
from coa_serve.tier3.agentic.planner import (
    _PROPOSE_SYSTEM,
    PLANNER_PROFILE_LEGACY,
    PLANNER_PROFILE_STRUCTURED_FIRST,
    PLANNER_PROFILES,
    BedrockStepPlanner,
    propose_system_for,
)
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.trace import TraceCollector

NS = "ppm360-kb-test"

ONTOLOGY_ITEM = {
    "type": "ontology",
    "node_types": ["Project", "StageGateMemo", "Decision"],
    "edge_types": ["DECIDES_ON", "MENTIONS"],
    "edge_map": [["DECIDES_ON", "StageGateMemo", "Project"]],
}

TABULAR_SQ = "What is the full cost of project PI0013123 from the data vault?"
DOCUMENT_SQ = "What did the latest stage-gate memo decide about project PI0013123?"


# ── deterministic doubles (shapes mirror test_tier3_agentic_e2e_properties) ──


class FakeClockSource:
    """A manually advanced monotonic time source (seconds) for the BudgetClock."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t


class _Chunk:
    def __init__(self, chunk_id: str) -> None:
        self.chunk_id = chunk_id
        self.text = f"text-{chunk_id}"
        self.source_doc = "memo.pdf"
        self.label = "L"
        self.relevance_score = 0.9
        self.uri = ""


class FakeTool:
    """A Tool double recording every invocation; returns a fixed result or raises."""

    def __init__(
        self, name: str, *, result: ToolResult | None = None, raises: bool = False, input_schema: dict | None = None
    ) -> None:
        self.name = name
        self.description = f"fake tool {name}"
        self.input_schema: dict = input_schema or {}
        self.output_schema: dict = {}
        self._result = result
        self._raises = raises
        self.calls: list[dict] = []

    async def invoke(self, *, namespace: str, **kwargs) -> ToolResult:
        self.calls.append({"namespace": namespace, **kwargs})
        if self._raises:
            raise RuntimeError(f"{self.name} boom")
        if self._result is not None:
            return self._result
        return ToolResult(chunks=(_Chunk(f"{self.name}-c"),), detail="ok")


class RecordingPlanner:
    """A planner that proposes nothing and records what each ``propose_steps`` saw."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def propose_steps(self, *, sub_question, context, tool_specs):
        self.seen.append({"profile": context.planner_profile, "ontology": context.ontology})
        return []

    async def check_sufficiency(self, *, sub_question, context):
        return False


class SchemaFirstPlanner:
    """Mimics the observed LLM: look the ontology up when the fork has none, then route.

    Routes the tabular sub-question to ``nl_to_sql`` and the document one to
    ``graph_traversal``; declares a sub-question sufficient once its tool has run.
    Deterministic and independent of sub-question interleaving.
    """

    def __init__(self) -> None:
        self.routed: set[str] = set()

    async def propose_steps(self, *, sub_question, context, tool_specs):
        if context.ontology is None:
            return [PlannerDecision(tool_name="ontology_lookup", inputs={}, reasoning="learn the schema first")]
        self.routed.add(sub_question.text)
        tool = "nl_to_sql" if sub_question.text == TABULAR_SQ else "graph_traversal"
        return [PlannerDecision(tool_name=tool, inputs={"query": sub_question.text}, reasoning="route by modality")]

    async def check_sufficiency(self, *, sub_question, context):
        return sub_question.text in self.routed


class FakeDecomposer:
    def __init__(self, sub_questions: list[SubQuestion]) -> None:
        self._sub_questions = sub_questions

    async def decompose(self, query, trace=None):
        if trace is not None:
            trace.record("decompose", "success", 0, detail=f"{len(self._sub_questions)} sub-questions")
        return list(self._sub_questions)


class FakeSynthesizer:
    async def synthesize(self, query, chunks, entities, **_kwargs):
        return SynthesisResult(answer="final answer", confidence=0.8, duration_ms=7)


def ontology_tool(**kwargs) -> FakeTool:
    return FakeTool("ontology_lookup", result=ToolResult(items=(ONTOLOGY_ITEM,), detail="ontology"), **kwargs)


def make_retriever(
    *,
    planner,
    tools: list[FakeTool],
    sub_questions: list[str] | None = None,
    max_steps: int = 10,
    planner_profile: str = PLANNER_PROFILE_LEGACY,
) -> AgenticRetriever:
    """A real AgenticRetriever over a real controller + registry with fake tools."""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgenticRetriever(
        registry=registry,
        controller=ReasoningController(planner, per_tool_timeout_s=5.0),
        decomposer=FakeDecomposer([SubQuestion(text=t) for t in (sub_questions or ["q1"])]),
        synthesizer=FakeSynthesizer(),
        budget=AgenticBudgetConfig(time_budget_s=300, max_steps=max_steps),
        clock_source=FakeClockSource(),
        final_semantic_search=False,
        planner_profile=planner_profile,
    )


def steps(trace: TraceCollector, name: str) -> list:
    return [s for s in trace.steps if s.step == name]


def stop_detail(trace: TraceCollector) -> str:
    stops = steps(trace, "agentic_retrieval_stop")
    assert len(stops) == 1
    return str(stops[0].detail)


# ── A. planner prompt per profile ──────────────────────────────────────────


@pytest.mark.unit
class TestProposeSystemFor:
    def test_legacy_is_the_unchanged_prompt(self):
        assert propose_system_for(PLANNER_PROFILE_LEGACY) is _PROPOSE_SYSTEM

    @pytest.mark.parametrize("value", [None, "", "bogus"])
    def test_unknown_or_empty_falls_back_to_legacy(self, value):
        assert propose_system_for(value) is _PROPOSE_SYSTEM

    def test_structured_first_replaces_the_standing_strategy(self):
        system = propose_system_for(PLANNER_PROFILE_STRUCTURED_FIRST)
        assert system != _PROPOSE_SYSTEM
        assert "FIRST, DECIDE THE MODALITY" in system
        assert "'nl_to_sql'" in system
        # The graph-first mandate is scoped, not stacked under a second rule.
        assert "(most questions)" not in system
        assert "GRAPH STRATEGY — for DOCUMENT / RELATIONSHIP sub-questions" in system
        # The rest of the prompt (tool catalog rules, output contract) is kept.
        assert system.endswith(_PROPOSE_SYSTEM[-80:])

    def test_profiles_set(self):
        assert frozenset({"legacy", "structured_first"}) == PLANNER_PROFILES


class _LLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def converse(self, *, prompt, system=None, guardrail_id=None, guard_content=None, temperature=None, **_):
        self.calls.append({"system": system})

        class _R:
            text = '{"candidates": []}'
            guardrail_blocked = False

        return _R()


_SPECS = [{"name": "nl_to_sql", "description": "SQL", "input_schema": {}, "output_schema": {}}]


@pytest.mark.unit
class TestBedrockPlannerUsesProfile:
    @pytest.mark.asyncio
    async def test_default_context_sends_legacy_prompt_byte_for_byte(self):
        llm = _LLM()
        await BedrockStepPlanner(llm).propose_steps(
            sub_question=SubQuestion(text="q"), context=AccumulatedContext(), tool_specs=_SPECS
        )
        assert llm.calls[0]["system"] == _PROPOSE_SYSTEM

    @pytest.mark.asyncio
    async def test_structured_first_context_sends_rewritten_prompt(self):
        llm = _LLM()
        ctx = AccumulatedContext(planner_profile=PLANNER_PROFILE_STRUCTURED_FIRST)
        await BedrockStepPlanner(llm).propose_steps(sub_question=SubQuestion(text="q"), context=ctx, tool_specs=_SPECS)
        assert llm.calls[0]["system"] == propose_system_for(PLANNER_PROFILE_STRUCTURED_FIRST)


@pytest.mark.unit
class TestContextCarriesProfile:
    def test_default_is_legacy(self):
        assert AccumulatedContext().planner_profile == PLANNER_PROFILE_LEGACY

    def test_fork_copies_profile(self):
        ctx = AccumulatedContext(planner_profile=PLANNER_PROFILE_STRUCTURED_FIRST)
        assert ctx.fork().planner_profile == PLANNER_PROFILE_STRUCTURED_FIRST


# ── B. retriever: profile resolution + ontology pre-resolve ────────────────


@pytest.mark.unit
class TestProfileResolution:
    def test_request_option_overrides_deployment_default(self):
        r = make_retriever(planner=RecordingPlanner(), tools=[])
        assert r._resolve_planner_profile({"plannerProfile": "structured_first"}) == "structured_first"

    def test_invalid_option_falls_back_to_deployment_default(self):
        r = make_retriever(planner=RecordingPlanner(), tools=[], planner_profile="structured_first")
        assert r._resolve_planner_profile({"plannerProfile": "bogus"}) == "structured_first"
        assert r._resolve_planner_profile({"plannerProfile": 3}) == "structured_first"
        assert r._resolve_planner_profile(None) == "structured_first"

    def test_invalid_constructor_value_falls_back_to_legacy(self):
        r = make_retriever(planner=RecordingPlanner(), tools=[], planner_profile="bogus")
        assert r._resolve_planner_profile(None) == PLANNER_PROFILE_LEGACY


@pytest.mark.unit
class TestOntologyPreresolve:
    @pytest.mark.asyncio
    async def test_legacy_does_not_preresolve_or_trace(self):
        onto = ontology_tool()
        planner = RecordingPlanner()
        trace = TraceCollector()
        await make_retriever(planner=planner, tools=[onto]).resolve("q", NS, trace=trace)
        assert onto.calls == []
        assert steps(trace, "ontology_preresolved") == []
        assert steps(trace, "planner_profile") == []
        assert planner.seen and all(s["profile"] == "legacy" and s["ontology"] is None for s in planner.seen)

    @pytest.mark.asyncio
    async def test_structured_first_preresolves_once_before_fan_out(self):
        onto = ontology_tool()
        planner = RecordingPlanner()
        trace = TraceCollector()
        await make_retriever(planner=planner, tools=[onto], sub_questions=["a", "b"]).resolve(
            "q", NS, trace=trace, options={"plannerProfile": "structured_first"}
        )
        assert len(onto.calls) == 1
        assert onto.calls[0]["namespace"] == NS
        # Every fork's planner call saw the resolved ontology and the profile.
        assert len(planner.seen) == 2
        for seen in planner.seen:
            assert seen["profile"] == "structured_first"
            assert seen["ontology"] is not None
            assert seen["ontology"].edge_types == ("DECIDES_ON", "MENTIONS")
        pre = steps(trace, "ontology_preresolved")
        assert [s.status for s in pre] == ["success"]
        assert "3 node types, 2 edge types" in str(pre[0].detail)
        assert str(steps(trace, "planner_profile")[0].detail) == "structured_first"
        # The pre-resolve never consumes a reasoning step: no ontology_lookup tool step.
        assert steps(trace, "ontology_lookup") == []

    @pytest.mark.asyncio
    async def test_deployment_default_applies_without_request_option(self):
        onto = ontology_tool()
        r = make_retriever(planner=RecordingPlanner(), tools=[onto], planner_profile="structured_first")
        await r.resolve("q", NS, trace=TraceCollector())
        assert len(onto.calls) == 1

    @pytest.mark.asyncio
    async def test_skipped_when_tool_absent(self):
        trace = TraceCollector()
        result = await make_retriever(planner=RecordingPlanner(), tools=[]).resolve(
            "q", NS, trace=trace, options={"plannerProfile": "structured_first"}
        )
        assert result is not None
        assert steps(trace, "ontology_preresolved") == []

    @pytest.mark.asyncio
    async def test_skipped_when_tool_withheld_for_request(self):
        onto = ontology_tool()
        trace = TraceCollector()
        await make_retriever(planner=RecordingPlanner(), tools=[onto]).resolve(
            "q",
            NS,
            trace=trace,
            options={"plannerProfile": "structured_first"},
            exclude_tools=frozenset({"ontology_lookup"}),
        )
        assert onto.calls == []
        assert steps(trace, "ontology_preresolved") == []

    @pytest.mark.asyncio
    async def test_failure_is_degraded_not_fatal(self):
        onto = ontology_tool(raises=True)
        trace = TraceCollector()
        result = await make_retriever(planner=RecordingPlanner(), tools=[onto]).resolve(
            "q", NS, trace=trace, options={"plannerProfile": "structured_first"}
        )
        pre = steps(trace, "ontology_preresolved")
        assert [s.status for s in pre] == ["failed"]
        assert "RuntimeError" in str(pre[0].detail)
        assert result.partial is True  # degraded source → partial honesty

    @pytest.mark.asyncio
    async def test_result_without_ontology_item_is_skipped(self):
        onto = FakeTool("ontology_lookup", result=ToolResult(detail="no ontology"))
        trace = TraceCollector()
        await make_retriever(planner=RecordingPlanner(), tools=[onto]).resolve(
            "q", NS, trace=trace, options={"plannerProfile": "structured_first"}
        )
        pre = steps(trace, "ontology_preresolved")
        assert [s.status for s in pre] == ["skipped"]


# ── regression pair: the observed failure under one shared step budget ──────


def _mixed_tools() -> dict[str, FakeTool]:
    query_schema = {"query": "str"}
    return {
        "ontology_lookup": ontology_tool(),
        "nl_to_sql": FakeTool(
            "nl_to_sql",
            input_schema=query_schema,
            result=ToolResult(rows=({"full_cost": "13322629.26"},), detail="1 row"),
        ),
        "graph_traversal": FakeTool("graph_traversal", input_schema=query_schema),
    }


@pytest.mark.unit
class TestMixedQuestionStepBudget:
    """Same planner, same tools, same ``max_steps=3``; only the profile differs.

    Under ``legacy`` each fork starts without an ontology, so both sub-questions
    spend a reasoning step on ``ontology_lookup`` before routing, and three steps
    are not enough for two routed tool calls: the session reports
    ``max_steps_reached``. Under ``structured_first`` the ontology is resolved
    before the fan-out at zero step cost, both sub-questions route on their first
    step, and the structured tool answers the tabular one.
    """

    @pytest.mark.asyncio
    async def test_legacy_spends_steps_on_ontology_lookups_and_exhausts_budget(self):
        tools = _mixed_tools()
        trace = TraceCollector()
        await make_retriever(
            planner=SchemaFirstPlanner(),
            tools=list(tools.values()),
            sub_questions=[TABULAR_SQ, DOCUMENT_SQ],
            max_steps=3,
        ).resolve("q", NS, trace=trace)
        assert len(steps(trace, "ontology_lookup")) >= 1  # paid for as reasoning steps
        assert str(StopReason.max_steps_reached) in stop_detail(trace)

    @pytest.mark.asyncio
    async def test_structured_first_routes_tabular_subquestion_to_sql_within_budget(self):
        tools = _mixed_tools()
        trace = TraceCollector()
        await make_retriever(
            planner=SchemaFirstPlanner(),
            tools=list(tools.values()),
            sub_questions=[TABULAR_SQ, DOCUMENT_SQ],
            max_steps=3,
        ).resolve("q", NS, trace=trace, options={"plannerProfile": "structured_first"})
        assert len(tools["ontology_lookup"].calls) == 1
        assert steps(trace, "ontology_lookup") == []
        sql = steps(trace, "nl_to_sql")
        assert [s.status for s in sql] == ["succeeded"]
        assert tools["nl_to_sql"].calls[0]["query"] == TABULAR_SQ
        assert [s.status for s in steps(trace, "graph_traversal")] == ["succeeded"]
        assert str(StopReason.max_steps_reached) not in stop_detail(trace)


# ── request model, config, factory ─────────────────────────────────────────


@pytest.mark.unit
class TestRequestModelValidation:
    @pytest.mark.parametrize("value", ["legacy", "structured_first"])
    def test_known_values_accepted(self, value):
        from coa_serve.models import InvokeRequest

        req = InvokeRequest(query="q", namespace="demo", options={"plannerProfile": value})
        assert req.options["plannerProfile"] == value

    @pytest.mark.parametrize("value", ["", "graph_first", "STRUCTURED_FIRST", 1, None])
    def test_unknown_values_rejected(self, value):
        from coa_serve.models import InvokeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="plannerProfile"):
            InvokeRequest(query="q", namespace="demo", options={"plannerProfile": value})

    def test_absent_is_fine(self):
        from coa_serve.models import InvokeRequest

        assert "plannerProfile" not in InvokeRequest(query="q", namespace="demo").options


@pytest.mark.unit
class TestServiceConfigEnv:
    def _load(self):
        from unittest.mock import MagicMock, patch

        with patch("coa_serve.config.boto3") as boto3:
            ssm = MagicMock()
            ssm.get_parameter.return_value = {"Parameter": {"Value": "gr-test"}}
            boto3.client.return_value = ssm
            from coa_serve.config import load_config

            return load_config()

    def test_default_is_legacy(self, monkeypatch):
        monkeypatch.delenv("DEEP_REASONING_PLANNER_PROFILE", raising=False)
        assert self._load().deep_reasoning_planner_profile == "legacy"

    def test_structured_first_honoured(self, monkeypatch):
        monkeypatch.setenv("DEEP_REASONING_PLANNER_PROFILE", " structured_first ")
        assert self._load().deep_reasoning_planner_profile == "structured_first"

    @pytest.mark.parametrize("value", ["bogus", "Legacy", ""])
    def test_invalid_falls_back_to_legacy(self, monkeypatch, value):
        monkeypatch.setenv("DEEP_REASONING_PLANNER_PROFILE", value)
        assert self._load().deep_reasoning_planner_profile == "legacy"


@pytest.mark.unit
class TestFactoryWiring:
    def test_profile_reaches_the_retriever(self, monkeypatch):
        from types import SimpleNamespace

        from coa_serve.config import ServiceConfig
        from coa_serve.tier3.agentic.factory import build_agentic_retriever

        monkeypatch.setenv("ENVIRONMENT", "local")
        config = ServiceConfig(
            guardrail_id="",
            vkg_endpoint="http://vkg.local:8080",
            neptune_endpoint="",
            opensearch_endpoint="",
            bedrock_model_id="model",
            bedrock_region="us-east-1",
            data_sources_table="",
            metric_definitions_table="",
            memory_id="",
            session_metadata_table="",
            deep_reasoning_planner_profile="structured_first",
        )
        clients = SimpleNamespace(graph=object(), vector=object(), llm=_LLM())
        retriever = build_agentic_retriever(config, clients, RecordingPlanner())
        assert retriever._planner_profile == "structured_first"


# ── lazy-import invariant (Req 12.3) ───────────────────────────────────────


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_planner_constants_do_not_import_graphrag(self):
        code = (
            "import sys\n"
            "from coa_serve.tier3.agentic.planner import PLANNER_PROFILES\n"
            "from coa_serve.tier3.agentic.retriever import AgenticRetriever\n"
            "assert not any(m.startswith('graphrag_toolkit') for m in sys.modules), 'graphrag imported'\n"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False)
        assert proc.returncode == 0, proc.stderr
