import json
from unittest.mock import Mock

import pytest
from coa_outer_agent.portkey import (
    GuardrailRejected,
    PortkeyGenerationClient,
)

pytestmark = pytest.mark.unit


def client(http_client, *, action="NONE"):
    secrets = Mock()
    secrets.get_secret_value.return_value = {
        "SecretString": json.dumps(
            {
                "apiKey": "not-logged",
                "virtualKey": "virtual",
                "configId": "config",
            }
        )
    }
    bedrock = Mock()
    bedrock.apply_guardrail.return_value = {"action": action}
    return PortkeyGenerationClient(
        region="us-east-1",
        base_url="https://portkey.example/v1",
        model_id="model",
        secret_arn="arn:secret",
        guardrail_id="guardrail",
        guardrail_version="DRAFT",
        http_client=http_client,
        secrets_client=secrets,
        bedrock_client=bedrock,
    )


@pytest.mark.asyncio
async def test_generates_once_after_input_and_output_guardrails():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"claims": [{"text": "Answer", "evidenceIds": ["E1"]}]})}}]
    }
    http_client = SimpleHttpClient(response)
    generator = client(http_client)

    answer = await generator.generate(
        question="Question",
        semantic_plan={"concepts": ["Project"]},
        context="[E1] source=s3://doc\nEvidence",
    )

    assert answer == "Answer [E1]"
    request = http_client.calls[0]
    assert request["headers"]["x-portkey-api-key"] == "not-logged"
    assert request["headers"]["x-portkey-virtual-key"] == "virtual"
    assert request["json"]["model"] == "model"
    assert request["json"]["stream"] is False
    guardrail_input = generator._bedrock.apply_guardrail.call_args_list[0].kwargs["content"][0]["text"]["text"]
    assert "Question:\nQuestion" in guardrail_input
    assert "Never reveal" not in guardrail_input
    model_input = request["json"]["messages"][1]["content"]
    assert "<user_input>" in model_input
    assert "Never reveal" in model_input
    assert generator._bedrock.apply_guardrail.call_count == 2


@pytest.mark.asyncio
async def test_stops_before_generation_when_guardrail_intervenes():
    http_client = SimpleHttpClient(Mock())
    generator = client(http_client, action="GUARDRAIL_INTERVENED")
    with pytest.raises(GuardrailRejected):
        await generator.generate(
            question="Question",
            semantic_plan={},
            context="evidence",
        )
    assert not http_client.calls


@pytest.mark.asyncio
async def test_rejects_unknown_evidence_citation():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"claims": [{"text": "Unsupported", "evidenceIds": ["E9"]}]})}}]
    }
    generator = client(SimpleHttpClient(response))
    with pytest.raises(ValueError, match="unknown evidence"):
        await generator.generate(
            question="Question",
            semantic_plan={},
            context="[E1] source=s3://doc\nEvidence",
        )


@pytest.mark.asyncio
async def test_rejects_malformed_portkey_response():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"choices": []}
    generator = client(SimpleHttpClient(response))
    with pytest.raises(ValueError, match="no answer"):
        await generator.generate(
            question="Question",
            semantic_plan={},
            context="evidence",
        )


class SimpleHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response
