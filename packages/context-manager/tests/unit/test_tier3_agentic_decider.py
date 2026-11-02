# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the typed Decider and its live settings provider (ADR 0014).

The Decider answers bounded, typed questions (a closed option list per question,
one chosen option plus a 0–1 confidence) over the deployment's generation client,
and NEVER raises into the reasoning loop: a disabled backend, a timeout, a model
error, a guardrail block, an unparseable reply or an option outside the list all
read as an abstention. Its settings are live: ``DeciderConfigProvider`` overlays
one SSM JSON parameter on the environment defaults on a TTL, so backend and
model change without a redeploy; a bad parameter is ignored (last-known-good,
else env defaults) rather than trusted.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from coa_serve.decider_config import (
    DECIDER_PARAMETER_SUFFIX,
    DeciderConfigProvider,
    settings_from_json,
)
from coa_serve.tier3.agentic.decider import (
    ABSTAIN,
    MODALITIES,
    Choice,
    Decider,
    DeciderSettings,
    LLMDecider,
    modality_questions,
    modality_state,
    parse_answers,
)

Q_MODALITY = modality_questions(2)
OPTIONS = MODALITIES


class FakeLLM:
    """``LLMClient.converse`` double returning scripted text, optionally slow or failing."""

    def __init__(self, *, text='{"answers": []}', blocked=False, raises=None, delay_s=0.0):
        self._text, self._blocked, self._raises, self._delay = text, blocked, raises, delay_s
        self.calls: list[dict] = []

    async def converse(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(text=self._text, guardrail_blocked=self._blocked)


def answers(*pairs: tuple[str, float]) -> str:
    return json.dumps({"answers": [{"id": f"q{i}", "option": o, "confidence": c} for i, (o, c) in enumerate(pairs, 1)]})


def decider(llm, **settings) -> LLMDecider:
    s = DeciderSettings(**{"backend": "llm", **settings})
    return LLMDecider(llm, settings_provider=lambda: s)


# ── parse_answers ───────────────────────────────────────────────────────────


@pytest.mark.unit
class TestParseAnswers:
    def test_valid_answers_map_to_choices(self):
        out = parse_answers(answers(("tabular", 0.93), ("document", 0.8)), Q_MODALITY)
        assert out == [Choice("tabular", 0.93), Choice("document", 0.8)]

    def test_option_matched_case_insensitively_and_canonicalised(self):
        out = parse_answers(answers(("TABULAR", 1), ("Document", 0.5)), Q_MODALITY)
        assert [c.option for c in out] == ["tabular", "document"]

    def test_unknown_option_is_abstention(self):
        out = parse_answers(answers(("graph", 0.9), ("document", 0.9)), Q_MODALITY)
        assert out[0] is ABSTAIN and out[0].abstained
        assert out[1].option == "document"

    def test_missing_answer_is_abstention(self):
        out = parse_answers(answers(("tabular", 0.9)), Q_MODALITY)
        assert out[1].abstained

    def test_tolerates_fences_and_prose(self):
        text = "Sure:\n```json\n" + answers(("mixed", 0.6), ("document", 0.7)) + "\n```"
        assert [c.option for c in parse_answers(text, Q_MODALITY)] == ["mixed", "document"]

    @pytest.mark.parametrize("raw,expected", [(1.7, 1.0), (-2, 0.0), ("high", 0.0), (None, 0.0)])
    def test_confidence_clamped_or_zeroed(self, raw, expected):
        text = json.dumps({"answers": [{"id": "q1", "option": "tabular", "confidence": raw}]})
        assert parse_answers(text, Q_MODALITY[:1])[0].confidence == expected

    def test_answers_without_ids_use_position(self):
        text = json.dumps(
            {"answers": [{"option": "tabular", "confidence": 0.9}, {"option": "mixed", "confidence": 0.5}]}
        )
        assert [c.option for c in parse_answers(text, Q_MODALITY)] == ["tabular", "mixed"]

    @pytest.mark.parametrize("text", ["", "not json", "[]", '{"answers": "x"}'])
    def test_garbage_is_all_abstentions(self, text):
        assert all(c.abstained for c in parse_answers(text, Q_MODALITY))


# ── settings ────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestDeciderSettings:
    def test_default_is_off(self):
        assert DeciderSettings().enabled is False

    def test_unknown_backend_normalises_to_off(self):
        assert DeciderSettings(backend="jev").normalised().backend == "off"

    def test_numbers_clamped(self):
        s = DeciderSettings(backend="llm", min_confidence=7, timeout_s=0, max_state_chars=1).normalised()
        assert (s.min_confidence, s.timeout_s, s.max_state_chars) == (1.0, 0.5, 500)

    def test_settings_from_json_overlays_defaults(self):
        defaults = DeciderSettings(backend="off", model_id="env-model", min_confidence=0.7)
        s = settings_from_json('{"backend": "llm", "modelId": "@portkey/jev-1.13", "timeoutS": 1.5}', defaults)
        assert s == DeciderSettings(backend="llm", model_id="@portkey/jev-1.13", min_confidence=0.7, timeout_s=1.5)

    @pytest.mark.parametrize("raw", ['{"backend": "jev"}', "[1]", "nope", '{"minConfidence": "x"}'])
    def test_settings_from_json_rejects_unusable(self, raw):
        with pytest.raises(ValueError):
            settings_from_json(raw, DeciderSettings())


# ── LLMDecider ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestLLMDecider:
    def test_is_a_decider(self):
        assert isinstance(decider(FakeLLM()), Decider)

    @pytest.mark.asyncio
    async def test_off_backend_abstains_without_calling_the_model(self):
        llm = FakeLLM()
        d = LLMDecider(llm, settings_provider=lambda: DeciderSettings(backend="off"))
        out = await d.choose_many(state="s", questions=Q_MODALITY)
        assert out == [ABSTAIN, ABSTAIN] and llm.calls == []

    @pytest.mark.asyncio
    async def test_empty_questions_short_circuit(self):
        llm = FakeLLM()
        assert await decider(llm).choose_many(state="s", questions=[]) == []
        assert llm.calls == []

    @pytest.mark.asyncio
    async def test_call_shape_state_options_temperature_zero(self):
        llm = FakeLLM(text=answers(("tabular", 0.9), ("document", 0.9)))
        out = await decider(llm, model_id="nova-micro").choose_many(
            state="THE STATE", questions=Q_MODALITY, guard_content="user text"
        )
        call = llm.calls[0]
        assert "THE STATE" in call["prompt"]
        assert "options: tabular | document | mixed" in call["prompt"]
        assert call["temperature"] == 0.0
        assert call["model_id"] == "nova-micro"
        assert call["guard_content"] == "user text"
        assert "classifier" in call["system"].lower()
        assert [c.option for c in out] == ["tabular", "document"]

    @pytest.mark.asyncio
    async def test_request_model_id_outranks_settings(self):
        llm = FakeLLM(text=answers(("tabular", 0.9)))
        await decider(llm, model_id="settings-model").choose_many(
            state="s", questions=Q_MODALITY[:1], model_id="request-model"
        )
        assert llm.calls[0]["model_id"] == "request-model"

    @pytest.mark.asyncio
    async def test_empty_model_id_means_client_default(self):
        llm = FakeLLM(text=answers(("tabular", 0.9)))
        await decider(llm, model_id="").choose_many(state="s", questions=Q_MODALITY[:1])
        assert llm.calls[0]["model_id"] is None

    @pytest.mark.asyncio
    async def test_state_is_truncated_to_max_chars(self):
        llm = FakeLLM(text=answers(("tabular", 0.9)))
        await decider(llm, max_state_chars=500).choose_many(state="§" * 5000, questions=Q_MODALITY[:1])
        assert llm.calls[0]["prompt"].count("§") == 500

    @pytest.mark.asyncio
    async def test_timeout_abstains(self):
        llm = FakeLLM(text=answers(("tabular", 0.9)), delay_s=0.2)
        out = await decider(llm, timeout_s=0.5).choose_many(state="s", questions=Q_MODALITY[:1])
        assert out[0].option == "tabular"  # 0.2 s fits the 0.5 s floor
        slow = FakeLLM(text=answers(("tabular", 0.9)), delay_s=0.8)
        out = await decider(slow, timeout_s=0.5).choose_many(state="s", questions=Q_MODALITY[:1])
        assert out == [ABSTAIN]

    @pytest.mark.asyncio
    async def test_model_error_abstains(self):
        out = await decider(FakeLLM(raises=RuntimeError("boom"))).choose_many(state="s", questions=Q_MODALITY)
        assert out == [ABSTAIN, ABSTAIN]

    @pytest.mark.asyncio
    async def test_guardrail_block_abstains(self):
        out = await decider(FakeLLM(blocked=True)).choose_many(state="s", questions=Q_MODALITY)
        assert out == [ABSTAIN, ABSTAIN]

    @pytest.mark.asyncio
    async def test_settings_provider_failure_reads_as_off(self):
        def boom():
            raise RuntimeError("ssm down")

        llm = FakeLLM()
        d = LLMDecider(llm, settings_provider=boom)
        assert d.settings().enabled is False
        assert await d.choose_many(state="s", questions=Q_MODALITY) == [ABSTAIN, ABSTAIN]
        assert llm.calls == []

    @pytest.mark.asyncio
    async def test_settings_are_read_live_per_call(self):
        """Flipping the provider between calls changes behaviour without rebuilding."""
        current = {"s": DeciderSettings(backend="off")}
        llm = FakeLLM(text=answers(("tabular", 0.9)))
        d = LLMDecider(llm, settings_provider=lambda: current["s"])
        assert (await d.choose_many(state="s", questions=Q_MODALITY[:1]))[0].abstained
        current["s"] = DeciderSettings(backend="llm", model_id="m2")
        assert (await d.choose_many(state="s", questions=Q_MODALITY[:1]))[0].option == "tabular"
        assert llm.calls[0]["model_id"] == "m2"


# ── modality helpers ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestModalityHelpers:
    def test_one_question_per_sub_question_with_closed_options(self):
        qs = modality_questions(3)
        assert len(qs) == 3
        assert all(opts == MODALITIES for _q, opts in qs)
        assert "sub-question 2" in qs[1][0]
        assert "data vault" in qs[0][0]  # warehouse wording is named as tabular

    def test_state_lists_question_subquestions_tools_and_classes(self):
        s = modality_state(
            question="Q?",
            sub_questions=["a", "b"],
            structured_tools=["nl_to_sql"],
            class_names=[f"C{i}" for i in range(60)],
        )
        assert "Original question: Q?" in s and "  1. a" in s and "  2. b" in s
        assert "nl_to_sql" in s
        assert "C39" in s and "C40" not in s  # capped at 40 class names

    def test_state_says_none_when_no_structured_tools(self):
        assert "none" in modality_state(question="Q", sub_questions=["a"], structured_tools=[])


# ── DeciderConfigProvider (live SSM overlay) ───────────────────────────────


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


ENV = DeciderSettings(backend="off", model_id="env-model")


def provider(getter, clock=None, ttl=60.0):
    return DeciderConfigProvider("/coa", ENV, ttl_s=ttl, time_fn=clock or FakeClock(), ssm_getter=getter)


@pytest.mark.unit
class TestDeciderConfigProvider:
    def test_parameter_path(self):
        assert provider(lambda n, required: "").parameter_name == "/coa" + DECIDER_PARAMETER_SUFFIX

    def test_absent_parameter_yields_env_defaults(self):
        seen = []

        def getter(name, *, required):
            seen.append((name, required))
            return ""

        assert provider(getter).settings() == ENV
        assert seen == [("/coa/deep-reasoning/decider", False)]

    def test_parameter_overlays_env_defaults(self):
        p = provider(lambda n, required: '{"backend": "llm", "modelId": "jev-via-portkey"}')
        s = p.settings()
        assert s.backend == "llm" and s.model_id == "jev-via-portkey" and s.min_confidence == 0.7

    def test_bad_first_read_falls_back_to_env_defaults(self):
        assert provider(lambda n, required: "{not json").settings() == ENV

    def test_unknown_backend_in_parameter_is_ignored_not_trusted(self):
        assert provider(lambda n, required: '{"backend": "jev"}').settings() == ENV

    def test_ssm_failure_keeps_last_known_good_and_retries(self):
        clock = FakeClock()
        values = iter(['{"backend": "llm"}', RuntimeError("throttled"), '{"backend": "off"}'])

        def getter(name, *, required):
            v = next(values)
            if isinstance(v, Exception):
                raise v
            return v

        p = provider(getter, clock)
        assert p.settings().backend == "llm"
        clock.t += 61
        assert p.settings().backend == "llm"  # failure → cached, TTL not advanced
        assert p.settings().backend == "off"  # immediate retry picks the new value

    def test_cached_within_ttl_and_refreshed_after(self):
        clock = FakeClock()
        calls = []

        def getter(name, *, required):
            calls.append(clock.t)
            return '{"backend": "llm"}' if len(calls) == 1 else '{"backend": "off"}'

        p = provider(getter, clock, ttl=60)
        assert p.settings().backend == "llm"
        clock.t += 30
        assert p.settings().backend == "llm" and len(calls) == 1
        clock.t += 31
        assert p.settings().backend == "off" and len(calls) == 2

    def test_prime_performs_first_read(self):
        calls = []

        def getter(name, *, required):
            calls.append(name)
            return ""

        provider(getter).prime()
        assert calls == ["/coa/deep-reasoning/decider"]
