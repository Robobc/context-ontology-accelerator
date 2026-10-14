# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Independent Bedrock Guardrails policy for non-Bedrock model gateways."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

import boto3
import structlog
from botocore.config import Config as BotoConfig
from coa_common import resolve_region
from coa_common.guardrail_metrics import (
    COMPONENT_NL_TO_SPARQL,
    DECISION_ALLOW,
    DECISION_ANONYMIZED,
    DECISION_BLOCK,
    DECISION_UNKNOWN,
    emit_guardrail_decision,
    filter_type_from_assessments,
)

from .base import GuardrailOutcome

logger = structlog.get_logger(__name__)

_DECISION_BY_OUTCOME = {
    GuardrailOutcome.NONE: DECISION_ALLOW,
    GuardrailOutcome.ANONYMIZED: DECISION_ANONYMIZED,
    GuardrailOutcome.BLOCKED: DECISION_BLOCK,
    GuardrailOutcome.UNKNOWN: DECISION_UNKNOWN,
}


@dataclass(frozen=True)
class GuardrailResult:
    """Safe text and classified outcome from an ApplyGuardrail evaluation."""

    text: str
    outcome: GuardrailOutcome


def _guardrail_output_text(response: dict[str, Any]) -> str:
    outputs = response.get("outputs") or []
    if not isinstance(outputs, list):
        return ""
    return "".join(
        output.get("text", "") for output in outputs if isinstance(output, dict) and isinstance(output.get("text"), str)
    )


def _guardrail_actions(value: Any) -> set[str]:
    """Collect action labels from all current and future policy shapes."""
    actions: set[str] = set()
    if isinstance(value, dict):
        action = value.get("action")
        if isinstance(action, str):
            actions.add(action)
        for nested in value.values():
            actions.update(_guardrail_actions(nested))
    elif isinstance(value, list):
        for nested in value:
            actions.update(_guardrail_actions(nested))
    return actions


class ApplyGuardrailPolicy:
    """Screen gateway input/output without coupling safety to that gateway."""

    def __init__(
        self,
        *,
        region: str | None = None,
        guardrail_version: str = "DRAFT",
        client: Any | None = None,
    ) -> None:
        """Configure the Bedrock region/version and an optional test client."""
        self._region = region or resolve_region()
        self._guardrail_version = guardrail_version
        self._client = client
        self._client_lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = boto3.client(
                        "bedrock-runtime",
                        region_name=self._region,
                        config=BotoConfig(
                            read_timeout=10,
                            connect_timeout=5,
                            retries={"max_attempts": 2},
                        ),
                    )
        return self._client

    async def apply(
        self,
        text: str,
        *,
        guardrail_id: str,
        source: Literal["INPUT", "OUTPUT"],
    ) -> GuardrailResult:
        """Evaluate text and fail closed when an intervention is unreadable."""
        if not text:
            return GuardrailResult(text="", outcome=GuardrailOutcome.NONE)

        started = time.monotonic()
        response = await asyncio.to_thread(
            self._get_client().apply_guardrail,
            guardrailIdentifier=guardrail_id,
            guardrailVersion=self._guardrail_version,
            source=source,
            content=[{"text": {"text": text}}],
            outputScope="FULL",
        )
        action = response.get("action", "NONE")
        assessments = response.get("assessments") or []
        if not isinstance(assessments, list):
            assessments = []
        if action == "NONE":
            return self._finish(
                GuardrailResult(text=text, outcome=GuardrailOutcome.NONE),
                assessments=assessments,
                started=started,
            )

        actions = _guardrail_actions(assessments)
        blocked = "BLOCKED" in actions
        safe_text = _guardrail_output_text(response)
        if blocked:
            logger.info("independent_guardrail_intervened", source=source, outcome="blocked")
            return self._finish(
                GuardrailResult(text=safe_text, outcome=GuardrailOutcome.BLOCKED),
                assessments=assessments,
                started=started,
            )
        # An intervention is benign only when every action is one we understand
        # and at least one is explicit anonymization. A future policy/action must
        # not silently become permission.
        if not safe_text or "ANONYMIZED" not in actions or not actions.issubset({"NONE", "ANONYMIZED"}):
            logger.warning(
                "independent_guardrail_unclassifiable",
                source=source,
                action=action,
            )
            return self._finish(
                GuardrailResult(text="", outcome=GuardrailOutcome.UNKNOWN),
                assessments=assessments,
                started=started,
            )

        logger.info("independent_guardrail_intervened", source=source, outcome="anonymized")
        return self._finish(
            GuardrailResult(text=safe_text, outcome=GuardrailOutcome.ANONYMIZED),
            assessments=assessments,
            started=started,
        )

    @staticmethod
    def _finish(
        result: GuardrailResult,
        *,
        assessments: list[dict[str, Any]],
        started: float,
    ) -> GuardrailResult:
        """Emit the same guardrail telemetry as native Bedrock generation."""
        emit_guardrail_decision(
            component=COMPONENT_NL_TO_SPARQL,
            blocked=result.outcome is GuardrailOutcome.BLOCKED,
            latency_ms=(time.monotonic() - started) * 1000,
            filter_type=filter_type_from_assessments(assessments),
            transport="emf",
            decision=_DECISION_BY_OUTCOME[result.outcome],
        )
        return result

    async def close(self) -> None:
        """Release the lazily-created boto3 client."""
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None
