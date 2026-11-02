# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fork (ADR 0015): Tier-2 skip evaluator backed by the ADR 0014 Decider.

Standard mode is a fixed cascade (metric → structured → knowledge) that stops at
the first tier that answers. On a MIXED namespace a document question ("what did
the committee decide and what condition did it attach") reaches Tier 2 first;
when the ontology happens to carry columns with matching names the generated SQL
"answers" and the document is never read. The AOSS provenance evaluator cannot
catch this: those columns ARE mapped, so it returns ``RUN``.

This evaluator asks the same Decider the ``seeded`` planner profile uses, with
the whole question as the single sub-question. A ``document`` tag at or above
the live ``min_confidence`` is positive evidence that Tier 2 cannot contribute
and yields ``SKIP``; ``tabular`` / ``mixed`` yield ``RUN``; an abstention, a
disabled backend or any failure fall through to the optional ``fallback``
evaluator (the AOSS one) or ``ABSTAIN``. Fail-open end to end, per the
:class:`~coa_serve.tier2.skip.evaluator.Tier2SkipEvaluator` contract.

Wiring is opt-in through ``SERVE_TIER2_SKIP_MODE=decider`` or ``decider+aoss``
(default stays ``aoss``). The live on/off switch is the Decider's own SSM
parameter: ``backend=off`` makes this evaluator defer to the fallback.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from ...step_ids import StepId
from ...tier3.agentic.decider import (
    MODALITY_DOCUMENT,
    MODALITY_MIXED,
    MODALITY_TABULAR,
    Choice,
    Decider,
    modality_questions,
    modality_state,
)
from .evaluator import Tier2SkipDecision, Tier2SkipEvaluator

if TYPE_CHECKING:
    from ...trace import TraceCollector

logger = structlog.get_logger(__name__)

EVALUATOR_NAME = "decider"
# Headroom over the Decider's own timeout so a hung call cannot stall the cascade.
_TIMEOUT_HEADROOM_S = 1.0


class DeciderTier2SkipEvaluator:
    """Prune Tier 2 when the Decider tags the whole question as a document question."""

    def __init__(
        self,
        decider: Decider,
        *,
        fallback: Tier2SkipEvaluator | None = None,
        structured_tools: tuple[str, ...] = ("nl_to_sql",),
    ) -> None:
        """Bind the Decider and an optional fallback consulted on abstention."""
        self._decider = decider
        self._fallback = fallback
        self._structured_tools = structured_tools

    async def should_skip_tier2(
        self,
        query: str,
        namespace: str,
        embedding: list[float] | None,
        *,
        trace: TraceCollector | None = None,
    ) -> Tier2SkipDecision:
        """Return SKIP on a confident ``document`` tag; otherwise RUN, fallback or ABSTAIN."""
        try:
            decision, detail = await self._evaluate(query)
        except Exception as exc:  # noqa: BLE001 - fail-open: never let the gate break the cascade
            logger.warning("tier2_skip_decider_error", error=type(exc).__name__)
            decision, detail = Tier2SkipDecision.ABSTAIN, {"evaluator": EVALUATOR_NAME, "error": type(exc).__name__}
        if trace is not None and decision is not Tier2SkipDecision.ABSTAIN:
            trace.record(StepId.ROUTING_SELECT, "success", int(detail.pop("duration_ms", 0)), detail=detail)
        if decision is Tier2SkipDecision.ABSTAIN and self._fallback is not None:
            return await self._fallback.should_skip_tier2(query, namespace, embedding, trace=trace)
        return decision

    async def _evaluate(self, query: str) -> tuple[Tier2SkipDecision, dict]:
        settings = self._decider.settings()
        if not settings.enabled:
            return Tier2SkipDecision.ABSTAIN, {"evaluator": EVALUATOR_NAME, "reason": "backend off"}
        state = modality_state(question=query, sub_questions=[query], structured_tools=self._structured_tools)
        started = time.perf_counter()
        choices: list[Choice] = await asyncio.wait_for(
            self._decider.choose_many(state=state, questions=modality_questions(1), guard_content=query),
            timeout=settings.timeout_s + _TIMEOUT_HEADROOM_S,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        choice = choices[0] if choices else None
        if choice is None or choice.abstained:
            return Tier2SkipDecision.ABSTAIN, {"evaluator": EVALUATOR_NAME, "reason": "abstained"}
        detail = {
            "evaluator": EVALUATOR_NAME,
            "modality": choice.option,
            "confidence": round(choice.confidence, 2),
            "model": settings.model_id,
            "duration_ms": duration_ms,
        }
        if choice.option == MODALITY_DOCUMENT and choice.confidence >= settings.min_confidence:
            return Tier2SkipDecision.SKIP, {**detail, "decision": "skip_tier2"}
        if choice.option in (MODALITY_TABULAR, MODALITY_MIXED):
            return Tier2SkipDecision.RUN, {**detail, "decision": "run_tier2"}
        return Tier2SkipDecision.ABSTAIN, {**detail, "reason": "low confidence"}
