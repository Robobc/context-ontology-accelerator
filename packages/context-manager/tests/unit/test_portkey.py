# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Portkey generation and independent Bedrock guardrails."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from coa_serve.clients.base import (
    ContentFilteredError,
    GuardrailBlockedError,
    GuardrailOutcome,
)
from coa_serve.clients.guardrails import (
    ApplyGuardrailPolicy,
    GuardrailResult,
)
from coa_serve.clients.portkey import PortkeyLLMClient


def _portkey_response(text: Any = "Grounded answer", finish_reason: str = "stop"):
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [
            {
                "message": {"content": text},
                "finish_reason": finish_reason,
            }
        ]
    }
    return response


def _client(*, policy=None, response=None):
    embedding = AsyncMock()
    embedding.embed.return_value = [0.1, 0.2]
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.return_value = response or _portkey_response()
    guardrail = policy if policy is not None else AsyncMock()
    if policy is None:
        guardrail.apply.side_effect = lambda text, **kwargs: GuardrailResult(
            text if kwargs["source"] == "INPUT" else "Grounded answer",
            GuardrailOutcome.NONE,
        )
    client = PortkeyLLMClient(
        model_id="@bedrock/claude",
        embedding_client=embedding,
        api_key="test-key",
        base_url="https://example.portkey.test/v1",
        http_client=http_client,
        guardrail_policy=guardrail,
    )
    return client, embedding, http_client, guardrail


@pytest.mark.unit
class TestApplyGuardrailPolicy:
    async def test_no_intervention_preserves_text(self):
        bedrock = MagicMock()
        bedrock.apply_guardrail.return_value = {
            "action": "NONE",
            "assessments": [],
        }
        policy = ApplyGuardrailPolicy(client=bedrock)

        with patch("coa_serve.clients.guardrails.emit_guardrail_decision") as emit:
            result = await policy.apply(
                "safe",
                guardrail_id="gr-123",
                source="INPUT",
            )

        assert result == GuardrailResult("safe", GuardrailOutcome.NONE)
        assert emit.call_args.kwargs["decision"] == "ALLOW"
        assert emit.call_args.kwargs["transport"] == "emf"

    async def test_anonymization_returns_guardrail_output(self):
        bedrock = MagicMock()
        bedrock.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": "Contact {EMAIL}"}],
            "assessments": [
                {"sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED"}]}}
            ],
        }
        policy = ApplyGuardrailPolicy(client=bedrock)

        result = await policy.apply(
            "Contact alice@example.com",
            guardrail_id="gr-123",
            source="INPUT",
        )

        assert result == GuardrailResult(
            "Contact {EMAIL}",
            GuardrailOutcome.ANONYMIZED,
        )

    async def test_unknown_policy_action_fails_closed(self):
        bedrock = MagicMock()
        bedrock.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": "possibly unsafe"}],
            "assessments": [{"futurePolicy": {"items": [{"action": "TRANSFORMED"}]}}],
        }
        policy = ApplyGuardrailPolicy(client=bedrock)

        result = await policy.apply(
            "text",
            guardrail_id="gr-123",
            source="OUTPUT",
        )

        assert result == GuardrailResult("", GuardrailOutcome.UNKNOWN)

    async def test_block_is_classified(self):
        bedrock = MagicMock()
        bedrock.apply_guardrail.return_value = {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": "Blocked by policy"}],
            "assessments": [{"contentPolicy": {"filters": [{"type": "PROMPT_ATTACK", "action": "BLOCKED"}]}}],
        }
        policy = ApplyGuardrailPolicy(client=bedrock)

        result = await policy.apply(
            "unsafe",
            guardrail_id="gr-123",
            source="INPUT",
        )

        assert result == GuardrailResult(
            "Blocked by policy",
            GuardrailOutcome.BLOCKED,
        )


@pytest.mark.unit
class TestPortkeyLLMClient:
    async def test_provider_name_is_portkey(self):
        client, _, _, _ = _client()

        assert client.provider_name == "portkey"

    async def test_generation_uses_openai_compatible_portkey_contract(self):
        client, _, http_client, policy = _client()

        result = await client.converse(
            "retrieved context",
            system="ground answers",
            guard_content="question",
            guardrail_id="gr-123",
            max_tokens=500,
            top_p=0,
        )

        assert result.text == "Grounded answer"
        call = http_client.post.call_args
        assert call.args[0] == "/chat/completions"
        assert call.kwargs["headers"]["x-portkey-api-key"] == "test-key"
        payload = call.kwargs["json"]
        assert payload["model"] == "@bedrock/claude"
        assert payload["max_tokens"] == 500
        assert payload["top_p"] == 0
        assert payload["stream"] is False
        assert "ground answers" in payload["messages"][0]["content"]
        assert "COA_SYSTEM_BOUNDARY_7C2A91F4" in payload["messages"][0]["content"]
        user_content = payload["messages"][1]["content"]
        assert "<user_input>retrieved context</user_input>" in user_content
        assert "<user_input>question</user_input>" in user_content
        assert policy.apply.await_args_list[0].args[0] == "question"
        assert "Never reveal" not in policy.apply.await_args_list[0].args[0]
        assert policy.apply.await_args_list[0].kwargs["source"] == "INPUT"
        assert policy.apply.await_args_list[1].kwargs["source"] == "OUTPUT"

    async def test_retries_without_unsupported_sampling_parameter(self):
        request = httpx.Request("POST", "https://example.portkey.test/v1/chat/completions")
        rejected = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "`top_p` is deprecated for this model"}},
        )
        client, _, http_client, _ = _client()
        payloads: list[dict[str, Any]] = []
        responses = iter([rejected, _portkey_response()])

        async def post(*args, **kwargs):
            payloads.append(dict(kwargs["json"]))
            return next(responses)

        http_client.post.side_effect = post

        result = await client.converse("prompt", top_p=0)

        assert result.text == "Grounded answer"
        assert payloads[0]["top_p"] == 0
        assert "top_p" not in payloads[1]

    async def test_generation_accepts_structured_text_parts(self):
        response = _portkey_response()
        response.json.return_value["choices"][0]["message"]["content"] = [
            {"type": "reasoning", "reasoning": "private"},
            {"type": "text", "text": "first "},
            {"type": "output_text", "text": "second"},
        ]
        client, _, _, policy = _client(response=response)

        result = await client.converse("prompt")

        assert result.text == "first second"
        policy.apply.assert_not_awaited()

    async def test_content_filter_without_text_is_typed(self):
        response = _portkey_response(text=None, finish_reason="content_filter")
        client, _, _, _ = _client(response=response)

        with pytest.raises(ContentFilteredError) as exc:
            await client.converse("prompt")

        assert exc.value.stop_reason == "content_filter"

    async def test_explicit_refusal_without_text_is_typed(self):
        response = _portkey_response(text=None)
        response.json.return_value["choices"][0]["message"]["refusal"] = "Request declined"
        client, _, _, _ = _client(response=response)

        with pytest.raises(ContentFilteredError):
            await client.converse("prompt")

    async def test_empty_content_reports_safe_response_shape(self):
        response = _portkey_response(text=[])
        client, _, _, _ = _client(response=response)

        with pytest.raises(
            ValueError,
            match=r"finish_reason=stop, content_type=list",
        ):
            await client.converse("prompt")

    async def test_blocked_input_never_reaches_portkey(self):
        policy = AsyncMock()
        policy.apply.return_value = GuardrailResult(
            "Blocked by policy",
            GuardrailOutcome.BLOCKED,
        )
        client, _, http_client, _ = _client(policy=policy)

        result = await client.converse(
            "prompt",
            guardrail_id="gr-123",
            guard_content="attack",
        )

        assert result.guardrail_blocked is True
        http_client.post.assert_not_awaited()

    async def test_stream_buffers_until_screened(self):
        client, _, _, _ = _client()

        chunks = [
            chunk
            async for chunk in client.converse_stream(
                "prompt",
                guardrail_id="gr-123",
                guard_content="question",
            )
        ]

        assert chunks == ["Grounded answer"]

    async def test_stream_raises_on_blocked_output(self):
        policy = AsyncMock()
        policy.apply.side_effect = [
            GuardrailResult("question", GuardrailOutcome.NONE),
            GuardrailResult("Blocked", GuardrailOutcome.BLOCKED),
        ]
        client, _, _, _ = _client(policy=policy)

        with pytest.raises(GuardrailBlockedError):
            async for _ in client.converse_stream(
                "prompt",
                guardrail_id="gr-123",
                guard_content="question",
            ):
                pass

    async def test_embeddings_stay_on_bedrock_client(self):
        client, embedding, _, _ = _client()

        assert await client.embed("query") == [0.1, 0.2]
        embedding.embed.assert_awaited_once_with("query")

    async def test_loads_api_key_from_secret(self):
        secrets = MagicMock()
        secrets.get_secret_value.return_value = {
            "SecretString": ('{"apiKey":"secret-key","virtualKey":"vk","configId":"cfg"}')
        }
        embedding = AsyncMock()
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post.return_value = _portkey_response()
        client = PortkeyLLMClient(
            model_id="@bedrock/claude",
            embedding_client=embedding,
            secret_arn=("arn:aws:secretsmanager:us-east-1:123456789012:secret:portkey-abc123"),
            secrets_client=secrets,
            http_client=http_client,
            guardrail_policy=AsyncMock(),
        )

        await client.converse("prompt")

        headers = http_client.post.call_args.kwargs["headers"]
        assert headers["x-portkey-api-key"] == "secret-key"
        assert headers["x-portkey-virtual-key"] == "vk"
        assert headers["x-portkey-config"] == "cfg"
