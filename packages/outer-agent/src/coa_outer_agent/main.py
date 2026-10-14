"""AgentCore Runtime entrypoint for the outer agent."""

from __future__ import annotations

import os
import re
import uuid

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from coa_common.constants import validate_query_text
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import OuterAgentConfig
from .identity import MissingIdentityError, from_runtime_context
from .mcp_client import McpJsonClient
from .orchestrator import OuterAgent, UnresolvedSemanticPlanError
from .portkey import GuardrailRejected, PortkeyGenerationClient
from .source_adapters import SourceAdapterRegistry, SourceExecutionError

logger = structlog.get_logger(__name__)
app = BedrockAgentCoreApp()

_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class ResolveRequest(BaseModel):
    """Public outer-agent request contract."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    namespace: str = Field(min_length=1, max_length=128)


_config: OuterAgentConfig | None = None
_agent: OuterAgent | None = None


def _get_agent() -> OuterAgent:
    global _config, _agent
    if _agent is not None:
        return _agent
    _config = OuterAgentConfig.from_env()
    generator = PortkeyGenerationClient(
        region=_config.region,
        base_url=_config.portkey_base_url,
        model_id=_config.portkey_model_id,
        secret_arn=_config.portkey_secret_arn,
        guardrail_id=_config.guardrail_id,
        guardrail_version=_config.guardrail_version,
    )
    source_adapters = SourceAdapterRegistry(
        region=_config.region,
        targets=_config.source_targets,
    )
    _agent = OuterAgent(
        coa_mcp_url=_config.coa_mcp_url,
        source_adapters=source_adapters,
        generator=generator,
        max_evidence_items=_config.max_evidence_items,
        max_evidence_characters=_config.max_evidence_characters,
        mcp_client_factory=McpJsonClient,
    )
    return _agent


@app.entrypoint
async def invoke(payload: dict, context=None):
    """Resolve one question through planning, retrieval, and generation."""
    request_id = str(uuid.uuid4())
    try:
        request = ResolveRequest.model_validate(payload)
        if not _NAMESPACE_PATTERN.fullmatch(request.namespace):
            raise ValueError("Invalid namespace format")
        identity = from_runtime_context(context)
        query = validate_query_text(request.query)
        result = await _get_agent().resolve(
            question=query,
            namespace=request.namespace,
            identity=identity,
        )
        yield {"requestId": request_id, **result}
    except MissingIdentityError as exc:
        yield {
            "requestId": request_id,
            "error": "Unauthorized",
            "message": str(exc),
            "statusCode": 401,
        }
    except UnresolvedSemanticPlanError as exc:
        yield {
            "requestId": request_id,
            "error": "SemanticPlanUnresolved",
            "message": str(exc),
            "statusCode": 409,
        }
    except SourceExecutionError as exc:
        logger.warning(
            "source_execution_failed",
            request_id=request_id,
            error_type=type(exc).__name__,
        )
        yield {
            "requestId": request_id,
            "error": "SourceUnavailable",
            "message": str(exc),
            "statusCode": 502,
        }
    except GuardrailRejected as exc:
        yield {
            "requestId": request_id,
            "error": "GuardrailRejected",
            "message": str(exc),
            "statusCode": 400,
        }
    except (ValidationError, ValueError) as exc:
        logger.warning(
            "outer_agent_request_rejected",
            request_id=request_id,
            error_type=type(exc).__name__,
        )
        yield {
            "requestId": request_id,
            "error": "ValidationError",
            "message": str(exc),
            "statusCode": 400,
        }
    except Exception as exc:
        logger.error(
            "outer_agent_failed",
            request_id=request_id,
            error_type=type(exc).__name__,
        )
        yield {
            "requestId": request_id,
            "error": "InternalError",
            "message": "Outer-agent resolution failed",
            "statusCode": 500,
        }


if __name__ == "__main__":
    os.environ.setdefault("PORT", "8080")
    app.run()
