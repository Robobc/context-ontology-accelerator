# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A typed Decider for the bounded decisions in deep reasoning (ADR 0014).

A large part of what the reasoning loop asks its planner is not language work
but a bounded, typed decision: *is this sub-question tabular or documentary?*
Today that decision is a sentence in the planner prompt (ADR 0013), which the
planner follows probabilistically and answers with a free-form paraphrase. The
Decider owns such decisions with the interface Jev-class classifiers made
familiar: a text ``state``, typed ``questions`` each with a closed list of
``options``, one chosen option plus a 0–1 confidence per question, several
questions answered in one call.

Backends are selected at call time from live :class:`DeciderSettings`, so an
operator can switch the decider off, on, or onto another model without a
redeploy (see ``coa_serve.decider_config``):

* ``off`` (default): nothing is called; every question is an abstention and the
  caller keeps its pre-ADR behaviour.
* ``llm``: :class:`LLMDecider` over the deployment's generation client
  (:class:`LLMClient`, so Bedrock or Portkey), ``temperature=0``, one JSON
  object as the only permitted output. ``model_id`` names the model; through
  Portkey that can be any model the gateway routes, a Jev-class classifier
  included, which is why no separate HTTP adapter exists.

A Decider never raises into the loop. A timeout, a parse failure, an unknown
option or a disabled backend all yield :data:`ABSTAIN`, and every caller treats
an abstention as "do what you did before this ADR existed".

Import discipline (Req 12.3): dependency-free; nothing here imports
``graphrag_toolkit`` or any backend client.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)

DECIDER_BACKEND_OFF = "off"
DECIDER_BACKEND_LLM = "llm"
DECIDER_BACKENDS: frozenset[str] = frozenset({DECIDER_BACKEND_OFF, DECIDER_BACKEND_LLM})

MODALITY_TABULAR = "tabular"
MODALITY_DOCUMENT = "document"
MODALITY_MIXED = "mixed"
MODALITIES: tuple[str, ...] = (MODALITY_TABULAR, MODALITY_DOCUMENT, MODALITY_MIXED)

_DEFAULT_MIN_CONFIDENCE = 0.7
_DEFAULT_TIMEOUT_S = 3.0
_DEFAULT_MAX_STATE_CHARS = 6000
_MAX_CLASS_NAMES_IN_STATE = 40


@dataclass(frozen=True)
class Choice:
    """One answered question: the chosen option (``""`` = abstained) and a 0–1 confidence."""

    option: str = ""
    confidence: float = 0.0

    @property
    def abstained(self) -> bool:
        """True when no option was chosen; callers fall back to their default path."""
        return not self.option


ABSTAIN = Choice()


@dataclass(frozen=True)
class DeciderSettings:
    """The live decider configuration one call runs under.

    ``model_id`` empty means the generation client's own default model. Unknown
    backends are normalised to ``off`` by :meth:`normalised` so a typo can never
    switch behaviour silently.
    """

    backend: str = DECIDER_BACKEND_OFF
    model_id: str = ""
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE
    timeout_s: float = _DEFAULT_TIMEOUT_S
    max_state_chars: int = _DEFAULT_MAX_STATE_CHARS

    @property
    def enabled(self) -> bool:
        """True when a backend other than ``off`` is selected."""
        return self.backend != DECIDER_BACKEND_OFF

    def normalised(self) -> DeciderSettings:
        """Return settings with an unknown backend replaced by ``off`` and numbers clamped."""
        backend = self.backend if self.backend in DECIDER_BACKENDS else DECIDER_BACKEND_OFF
        return DeciderSettings(
            backend=backend,
            model_id=(self.model_id or "").strip(),
            min_confidence=min(1.0, max(0.0, float(self.min_confidence))),
            timeout_s=max(0.5, float(self.timeout_s)),
            max_state_chars=max(500, int(self.max_state_chars)),
        )


@runtime_checkable
class Decider(Protocol):
    """The typed-decision surface the reasoning loop consults.

    ``choose_many`` answers several questions about one ``state`` in one call;
    ``questions`` is a sequence of ``(question, options)`` pairs and the result
    has exactly one :class:`Choice` per question, :data:`ABSTAIN` where no
    option could be chosen. ``guard_content`` is the user-derived part of the
    state to tag for guardrail evaluation (M-5 scoping), as the planner does.
    """

    def settings(self) -> DeciderSettings:
        """The live settings this decider would run under right now."""
        ...

    async def choose_many(
        self,
        *,
        state: str,
        questions: Sequence[tuple[str, Sequence[str]]],
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> list[Choice]:
        """Answer every question about ``state``; one :class:`Choice` per question, never raises."""
        ...


_SYSTEM = (
    "You are a classifier, not an assistant. You are given a STATE and one or more "
    "typed QUESTIONS about it. For EACH question choose exactly ONE option from that "
    "question's closed list, copied verbatim, and give your confidence as a number "
    "from 0 to 1. Never invent an option and never explain. If no option fits, answer "
    'with option "" and confidence 0.\n'
    "Respond with ONLY a JSON object of this exact shape and no prose outside it:\n"
    '{"answers": [{"id": "q1", "option": "<one option verbatim>", "confidence": <0..1>}, ...]}'
)


class LLMDecider:
    """A :class:`Decider` over an :class:`LLMClient` (Bedrock or Portkey).

    Reads :class:`DeciderSettings` from ``settings_provider`` on every call, so a
    live provider (SSM-backed) changes backend, model, timeout and threshold
    without a redeploy. ``guardrail_id_provider`` mirrors the planner's.
    """

    name = DECIDER_BACKEND_LLM

    def __init__(
        self,
        llm_client: Any,
        *,
        settings_provider: Callable[[], DeciderSettings],
        guardrail_id_provider: Callable[[], str] | None = None,
    ) -> None:
        """Bind the generation client and the live settings/guardrail providers."""
        self._llm = llm_client
        self._settings_provider = settings_provider
        self._guardrail_id_provider = guardrail_id_provider

    def settings(self) -> DeciderSettings:
        """The current (live) settings, normalised."""
        try:
            return self._settings_provider().normalised()
        except Exception as exc:  # noqa: BLE001 - a settings failure must read as "off", never raise
            logger.warning("decider_settings_unavailable", error=type(exc).__name__)
            return DeciderSettings()

    async def choose_many(
        self,
        *,
        state: str,
        questions: Sequence[tuple[str, Sequence[str]]],
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> list[Choice]:
        """Answer every question in one call; any failure yields abstentions (never raises)."""
        n = len(questions)
        if n == 0:
            return []
        settings = self.settings()
        if not settings.enabled:
            return [ABSTAIN] * n
        prompt = _render_prompt(state[: settings.max_state_chars], questions)
        guardrail_id = self._guardrail_id_provider() if self._guardrail_id_provider else None
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._llm.converse(
                    prompt,
                    system=_SYSTEM,
                    guardrail_id=guardrail_id or None,
                    guard_content=guard_content,
                    temperature=0.0,
                    max_tokens=512,
                    model_id=model_id or settings.model_id or None,
                ),
                timeout=settings.timeout_s,
            )
        except TimeoutError:
            logger.warning("decider_timeout", timeout_s=settings.timeout_s, questions=n)
            return [ABSTAIN] * n
        except Exception as exc:  # noqa: BLE001 - a decider must never raise into the loop
            logger.warning("decider_error", error=type(exc).__name__, error_msg=str(exc)[:200])
            return [ABSTAIN] * n
        if getattr(result, "guardrail_blocked", False):
            logger.warning("decider_guardrail_blocked", questions=n)
            return [ABSTAIN] * n
        choices = parse_answers(getattr(result, "text", "") or "", questions)
        logger.debug(
            "decider_answered",
            model_id=model_id or settings.model_id or "default",
            ms=int((time.monotonic() - started) * 1000),
            answers=[(c.option, round(c.confidence, 2)) for c in choices],
        )
        return choices


def _render_prompt(state: str, questions: Sequence[tuple[str, Sequence[str]]]) -> str:
    lines = [f"STATE:\n{state}\n", "QUESTIONS:"]
    for i, (question, options) in enumerate(questions, start=1):
        lines.append(f"q{i}: {question}")
        lines.append("   options: " + " | ".join(options))
    return "\n".join(lines)


def parse_answers(text: str, questions: Sequence[tuple[str, Sequence[str]]]) -> list[Choice]:
    """Map the model's ``{"answers": [...]}`` onto one validated :class:`Choice` per question.

    An answer is kept only when its ``option`` matches one of that question's
    options (case-insensitively, returned in the option's canonical spelling);
    everything else, including a missing answer, is :data:`ABSTAIN`. Confidence
    is clamped to 0–1 and non-numeric confidence reads as 0.
    """
    payload = _extract_json_object(text)
    answers = payload.get("answers") if isinstance(payload, dict) else None
    by_id: dict[str, dict] = {}
    if isinstance(answers, list):
        for pos, entry in enumerate(answers, start=1):
            if isinstance(entry, dict):
                by_id[str(entry.get("id") or f"q{pos}")] = entry
    choices: list[Choice] = []
    for i, (_question, options) in enumerate(questions, start=1):
        entry = by_id.get(f"q{i}")
        if entry is None:
            choices.append(ABSTAIN)
            continue
        raw_option = str(entry.get("option") or "").strip()
        canonical = next((o for o in options if o.lower() == raw_option.lower()), "")
        if not canonical:
            choices.append(ABSTAIN)
            continue
        try:
            confidence = min(1.0, max(0.0, float(entry.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        choices.append(Choice(option=canonical, confidence=confidence))
    return choices


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    candidate = text.strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        pass
    first, last = candidate.find("{"), candidate.rfind("}")
    if first == -1 or last <= first:
        return None
    try:
        parsed = json.loads(candidate[first : last + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ── the one decision the loop asks today: sub-question modality (ADR 0014 D1) ──

_MODALITY_QUESTION = (
    "Which modality answers sub-question {n}? '{tabular}' = it asks for a figure, amount, "
    "count, total, average, ranking, date or status held in the relational tables (a "
    "source called a data vault, warehouse, lakehouse, mart, database or by a table name "
    "is a relational table); '{document}' = it asks what documents say, decided or "
    "recommend, or how entities relate across documents; '{mixed}' = it needs both."
)


def modality_questions(sub_question_count: int) -> list[tuple[str, Sequence[str]]]:
    """One modality question per sub-question, options ``tabular | document | mixed``."""
    return [
        (
            _MODALITY_QUESTION.format(n=i, tabular=MODALITY_TABULAR, document=MODALITY_DOCUMENT, mixed=MODALITY_MIXED),
            MODALITIES,
        )
        for i in range(1, sub_question_count + 1)
    ]


def modality_state(
    *,
    question: str,
    sub_questions: Sequence[str],
    structured_tools: Sequence[str],
    class_names: Sequence[str] = (),
) -> str:
    """Render the state the modality decision is made on.

    The original question and its numbered sub-questions are the user-derived
    part; the structured tools available for this request and the namespace
    ontology's class names (capped) tell the classifier what the tables and the
    graph are about. Table business names are not included yet: the structured
    tier resolves them per call and the ontology classes already name the
    domain (ADR 0014, rollout step 1).
    """
    lines = [f"Original question: {question}", "Sub-questions:"]
    lines += [f"  {i}. {text}" for i, text in enumerate(sub_questions, start=1)]
    lines.append(
        "Structured (relational) tools available for this request: "
        + (", ".join(structured_tools) if structured_tools else "none")
    )
    if class_names:
        shown = list(class_names)[:_MAX_CLASS_NAMES_IN_STATE]
        lines.append("Ontology classes in this namespace: " + ", ".join(shown))
    return "\n".join(lines)
