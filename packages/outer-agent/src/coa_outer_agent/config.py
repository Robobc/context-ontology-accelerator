"""Environment configuration for the outer-agent runtime."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import quote

from .source_adapters import SourceTargetConfig


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KB_ID_RE = re.compile(r"^[A-Za-z0-9]{10}$")
_TARGET_KINDS = {"MANAGED_KB", "GOVERNED_QUERY", "EXTERNAL_GRAPH", "COA_GRAPH"}


def _source_targets() -> dict[str, SourceTargetConfig]:
    raw = _required("SOURCE_TARGETS_JSON")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SOURCE_TARGETS_JSON must be valid JSON") from exc
    if not isinstance(value, dict) or not 1 <= len(value) <= 32:
        raise ValueError("SOURCE_TARGETS_JSON must define 1-32 targets")

    targets: dict[str, SourceTargetConfig] = {}
    for target_id, item in value.items():
        if not isinstance(target_id, str) or not _TARGET_ID_RE.fullmatch(target_id):
            raise ValueError("SOURCE_TARGETS_JSON contains an invalid target ID")
        if not isinstance(item, dict):
            raise ValueError(f"Source target {target_id} must be an object")
        kind = item.get("kind")
        if kind not in _TARGET_KINDS:
            raise ValueError(f"Source target {target_id} has an invalid kind")
        allowed_fields = {"kind", "knowledgeBaseIds", "maxAgentIterations"} if kind == "MANAGED_KB" else {"kind"}
        if extra := set(item) - allowed_fields:
            raise ValueError(f"Source target {target_id} has unsupported fields: {sorted(extra)}")

        knowledge_base_ids = item.get("knowledgeBaseIds", [])
        if kind == "MANAGED_KB":
            if (
                not isinstance(knowledge_base_ids, list)
                or not 1 <= len(knowledge_base_ids) <= 10
                or any(not isinstance(kb_id, str) or not _KB_ID_RE.fullmatch(kb_id) for kb_id in knowledge_base_ids)
            ):
                raise ValueError(f"Managed KB target {target_id} requires 1-10 valid knowledgeBaseIds")
        elif knowledge_base_ids:
            raise ValueError(f"Source target {target_id} does not accept knowledgeBaseIds")

        max_iterations = item.get("maxAgentIterations", 5)
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= 20:
            raise ValueError(f"Source target {target_id} maxAgentIterations must be 1-20")
        targets[target_id] = SourceTargetConfig(
            target_id=target_id,
            kind=kind,
            knowledge_base_ids=tuple(knowledge_base_ids),
            max_agent_iterations=max_iterations,
        )
    return targets


@dataclass(frozen=True)
class OuterAgentConfig:
    """Validated runtime configuration."""

    region: str
    coa_mcp_url: str
    source_targets: dict[str, SourceTargetConfig]
    portkey_base_url: str
    portkey_model_id: str
    portkey_secret_arn: str
    guardrail_id: str
    guardrail_version: str
    group_claim_name: str
    max_evidence_items: int
    max_evidence_characters: int

    @classmethod
    def from_env(cls) -> OuterAgentConfig:
        """Load configuration from the AgentCore runtime environment."""
        region = os.environ.get("AWS_REGION", "us-east-1")
        runtime_arn = _required("COA_MCP_RUNTIME_ARN")
        encoded_arn = quote(runtime_arn, safe="")
        coa_mcp_url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        )
        return cls(
            region=region,
            coa_mcp_url=coa_mcp_url,
            source_targets=_source_targets(),
            portkey_base_url=os.environ.get("PORTKEY_BASE_URL", "https://api.portkey.ai/v1").rstrip("/"),
            portkey_model_id=_required("PORTKEY_MODEL_ID"),
            portkey_secret_arn=_required("PORTKEY_SECRET_ARN"),
            guardrail_id=os.environ.get("BEDROCK_GUARDRAIL_ID", "").strip(),
            guardrail_version=os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT"),
            group_claim_name=os.environ.get("GROUP_CLAIM_NAME", "groups"),
            max_evidence_items=_positive_int("MAX_EVIDENCE_ITEMS", 12),
            max_evidence_characters=_positive_int("MAX_EVIDENCE_CHARACTERS", 30000),
        )
