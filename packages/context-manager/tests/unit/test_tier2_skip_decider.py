# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fork (ADR 0015): Decider-backed Tier-2 skip evaluator.

The evaluator prunes Tier 2 only on a confident ``document`` tag, runs it on
``tabular`` / ``mixed``, and falls through (to the fallback evaluator or
``ABSTAIN``) on abstention, a disabled backend or any failure. Fail-open per the
``Tier2SkipEvaluator`` contract: it never raises.
"""

from __future__ import annotations

import pytest
from coa_serve.tier2.skip import Tier2SkipDecision
from coa_serve.tier2.skip.decider_skip import DeciderTier2SkipEvaluator
from coa_serve.tier3.agentic.decider import ABSTAIN, Choice, DeciderSettings
from coa_serve.trace import TraceCollector


class FakeDecider:
    """A ``Decider`` returning a scripted ``Choice`` (or raising)."""

    def __init__(self, choice: Choice | None, *, enabled: bool = True, raises: Exception | None = None) -> None:
        self._choice = choice
        self._enabled = enabled
        self._raises = raises
        self.calls: list[dict] = []

    def settings(self) -> DeciderSettings:
        return DeciderSettings(
            backend="llm" if self._enabled else "off", model_id="nova-micro", min_confidence=0.7, timeout_s=3.0
        ).normalised()

    async def choose_many(self, *, state, questions, guard_content=None, model_id=None):
        self.calls.append({"state": state, "questions": questions, "guard": guard_content})
        if self._raises is not None:
            raise self._raises
        return [self._choice] if self._choice is not None else []


class FakeFallback:
    """A fallback evaluator recording whether it was consulted."""

    def __init__(self, decision: Tier2SkipDecision) -> None:
        self._decision = decision
        self.calls = 0

    async def should_skip_tier2(self, query, namespace, embedding, *, trace=None):
        self.calls += 1
        return self._decision


Q = "What did the stage-gate committee decide about project PI0013025 and what condition did it attach?"


@pytest.mark.unit
class TestDeciderTier2SkipEvaluator:
    @pytest.mark.asyncio
    async def test_confident_document_tag_skips_tier2_and_traces(self):
        ev = DeciderTier2SkipEvaluator(FakeDecider(Choice("document", 0.95)))
        trace = TraceCollector()
        assert await ev.should_skip_tier2(Q, "ns", None, trace=trace) is Tier2SkipDecision.SKIP
        step = trace.steps[-1]
        assert step.step == "routing.select"
        assert step.detail["decision"] == "skip_tier2"
        assert step.detail["modality"] == "document"
        assert step.detail["model"] == "nova-micro"

    @pytest.mark.asyncio
    async def test_tabular_and_mixed_run_tier2(self):
        for modality in ("tabular", "mixed"):
            ev = DeciderTier2SkipEvaluator(FakeDecider(Choice(modality, 0.9)))
            assert await ev.should_skip_tier2(Q, "ns", None) is Tier2SkipDecision.RUN

    @pytest.mark.asyncio
    async def test_low_confidence_document_falls_through(self):
        fallback = FakeFallback(Tier2SkipDecision.SKIP)
        ev = DeciderTier2SkipEvaluator(FakeDecider(Choice("document", 0.5)), fallback=fallback)
        assert await ev.should_skip_tier2(Q, "ns", None) is Tier2SkipDecision.SKIP
        assert fallback.calls == 1

    @pytest.mark.asyncio
    async def test_abstention_without_fallback_abstains(self):
        ev = DeciderTier2SkipEvaluator(FakeDecider(ABSTAIN))
        assert await ev.should_skip_tier2(Q, "ns", None) is Tier2SkipDecision.ABSTAIN

    @pytest.mark.asyncio
    async def test_backend_off_defers_to_fallback_without_calling_the_model(self):
        decider = FakeDecider(Choice("document", 0.99), enabled=False)
        fallback = FakeFallback(Tier2SkipDecision.RUN)
        ev = DeciderTier2SkipEvaluator(decider, fallback=fallback)
        assert await ev.should_skip_tier2(Q, "ns", None) is Tier2SkipDecision.RUN
        assert decider.calls == []
        assert fallback.calls == 1

    @pytest.mark.asyncio
    async def test_decider_error_is_fail_open(self):
        ev = DeciderTier2SkipEvaluator(FakeDecider(None, raises=RuntimeError("boom")))
        trace = TraceCollector()
        assert await ev.should_skip_tier2(Q, "ns", None, trace=trace) is Tier2SkipDecision.ABSTAIN
        assert all(s.step != "routing.select" for s in trace.steps)

    @pytest.mark.asyncio
    async def test_whole_question_is_the_single_sub_question(self):
        decider = FakeDecider(Choice("document", 0.9))
        await DeciderTier2SkipEvaluator(decider).should_skip_tier2(Q, "ns", None)
        call = decider.calls[0]
        assert len(call["questions"]) == 1
        assert Q in call["state"]
        assert call["guard"] == Q

    @pytest.mark.asyncio
    async def test_namespace_without_documents_abstains_without_calling_the_model(self):
        decider = FakeDecider(Choice("document", 0.99))
        fallback = FakeFallback(Tier2SkipDecision.RUN)

        async def has_documents(namespace: str) -> bool:
            return namespace == "mixed-ns"

        ev = DeciderTier2SkipEvaluator(decider, fallback=fallback, has_documents=has_documents)
        assert await ev.should_skip_tier2(Q, "tables-only-ns", None) is Tier2SkipDecision.RUN
        assert decider.calls == [] and fallback.calls == 1
        assert await ev.should_skip_tier2(Q, "mixed-ns", None) is Tier2SkipDecision.SKIP
        assert len(decider.calls) == 1
