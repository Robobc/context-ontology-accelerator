from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from coa_outer_agent import main
from coa_outer_agent.portkey import GuardrailRejected

pytestmark = pytest.mark.unit


def context():
    token = jwt.encode({"sub": "s", "email": "person@example.com"}, "test-key")
    return SimpleNamespace(request_headers={"authorization": f"Bearer {token}"})


async def result_of(payload, runtime_context):
    return [item async for item in main.invoke(payload, context=runtime_context)][0]


@pytest.mark.asyncio
async def test_entrypoint_returns_outer_agent_result(monkeypatch):
    agent = SimpleNamespace(resolve=AsyncMock(return_value={"answer": "answer", "trace": []}))
    monkeypatch.setattr(main, "_get_agent", lambda: agent)
    result = await result_of({"query": "Question", "namespace": "project"}, context())
    assert result["answer"] == "answer"
    assert result["requestId"]
    assert agent.resolve.await_args.kwargs["identity"].user_id == ("person@example.com")


@pytest.mark.asyncio
async def test_entrypoint_rejects_body_identity_and_extra_fields():
    result = await result_of(
        {
            "query": "Question",
            "namespace": "project",
            "userContext": {"userId": "attacker"},
        },
        context(),
    )
    assert result["statusCode"] == 400


@pytest.mark.asyncio
async def test_entrypoint_requires_forwarded_token():
    result = await result_of(
        {"query": "Question", "namespace": "project"},
        SimpleNamespace(request_headers={}),
    )
    assert result["statusCode"] == 401


@pytest.mark.asyncio
async def test_entrypoint_reports_guardrail_rejection(monkeypatch):
    agent = SimpleNamespace(resolve=AsyncMock(side_effect=GuardrailRejected("blocked")))
    monkeypatch.setattr(main, "_get_agent", lambda: agent)
    result = await result_of({"query": "Question", "namespace": "project"}, context())
    assert result["error"] == "GuardrailRejected"


@pytest.mark.asyncio
async def test_entrypoint_hides_internal_failure(monkeypatch):
    agent = SimpleNamespace(resolve=AsyncMock(side_effect=RuntimeError("private detail")))
    monkeypatch.setattr(main, "_get_agent", lambda: agent)
    result = await result_of({"query": "Question", "namespace": "project"}, context())
    assert result["statusCode"] == 500
    assert "private detail" not in result["message"]
