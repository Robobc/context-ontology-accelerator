"""Outer-agent orchestration across COA, native retrieval, and Portkey."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from .evidence import assemble_context
from .identity import TrustedIdentity
from .mcp_client import McpJsonClient, McpToolError
from .plan_models import SemanticExecutionPlan
from .portkey import PortkeyGenerationClient
from .source_adapters import SourceAdapterRegistry, SourceExecutionError


class UnresolvedSemanticPlanError(ValueError):
    """Raised when COA does not return a resolved, executable plan."""


class OuterAgent:
    """Compose semantic planning, native retrieval, and final generation."""

    def __init__(
        self,
        *,
        coa_mcp_url: str,
        source_adapters: SourceAdapterRegistry,
        generator: PortkeyGenerationClient,
        max_evidence_items: int = 12,
        max_evidence_characters: int = 30000,
        mcp_client_factory=McpJsonClient,
    ):
        """Configure downstream endpoints and deterministic evidence limits."""
        self._coa_url = coa_mcp_url
        self._source_adapters = source_adapters
        self._generator = generator
        self._max_evidence_items = max_evidence_items
        self._max_evidence_characters = max_evidence_characters
        self._mcp_client_factory = mcp_client_factory

    async def resolve(
        self,
        *,
        question: str,
        namespace: str,
        identity: TrustedIdentity,
    ) -> dict[str, Any]:
        """Resolve a question while preserving identity and source ownership."""
        started = time.perf_counter()
        trace: list[dict[str, Any]] = []

        plan_started = time.perf_counter()
        try:
            raw = await self._mcp(identity, self._coa_url).call_tool(
                "semantic_execution_plan",
                {"query": question, "namespaceId": namespace},
            )
            semantic_plan = SemanticExecutionPlan.model_validate(_find_value(raw, "plan"))
        except (McpToolError, ValidationError) as exc:
            raise UnresolvedSemanticPlanError("COA did not return a valid semantic execution plan") from exc
        if semantic_plan.status != "RESOLVED":
            codes = ", ".join(item.code for item in semantic_plan.ambiguities)
            raise UnresolvedSemanticPlanError(
                f"Semantic execution plan requires clarification: {codes or semantic_plan.status}"
            )
        trace.append(
            _step(
                "coa.semantic_plan",
                plan_started,
                "resolved",
                {
                    "planVersion": semantic_plan.planVersion,
                    "ontologyVersion": semantic_plan.ontologyVersion,
                    "directives": len(semantic_plan.sourceDirectives),
                },
            )
        )

        source_started = time.perf_counter()
        try:
            batches = await asyncio.gather(
                *(self._source_adapters.execute(directive, identity) for directive in semantic_plan.sourceDirectives)
            )
        except SourceExecutionError:
            # Every directive represents expected evidence. Do not generate an
            # answer from a partial set when one source cannot enforce/execute.
            raise
        evidence = [item for batch in batches for item in batch]
        evidence = [replace(item, evidence_id=f"E{index}") for index, item in enumerate(evidence, start=1)]
        context, selected = assemble_context(
            evidence,
            max_items=self._max_evidence_items,
            max_characters=self._max_evidence_characters,
        )
        trace.append(
            _step(
                "sources.execute",
                source_started,
                "resolved",
                {"items": len(evidence), "selected": len(selected)},
            )
        )

        if not selected:
            answer = "Insufficient authorized evidence to answer the question."
            trace.append(_step("generation.skipped", time.perf_counter(), "no_evidence"))
        else:
            generate_started = time.perf_counter()
            answer = await self._generator.generate(
                question=question,
                semantic_plan=semantic_plan.model_dump(),
                context=context,
            )
            trace.append(_step("portkey.generate", generate_started, "resolved"))

        return {
            "answer": answer,
            "groundedQuery": semantic_plan.groundedQuestion,
            "semanticPlan": semantic_plan.model_dump(),
            "evidence": [item.to_dict() for item in selected],
            "trace": trace,
            "durationMs": int((time.perf_counter() - started) * 1000),
        }

    def _mcp(self, identity: TrustedIdentity, url: str) -> McpJsonClient:
        return self._mcp_client_factory(url, identity.bearer_token)


def _find_string(value: Any, key: str) -> str:
    found = _find_value(value, key)
    return found if isinstance(found, str) else ""


def _find_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for nested in value.values():
            found = _find_value(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_value(nested, key)
            if found is not None:
                return found
    return None


def _step(
    name: str,
    started: float,
    status: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "step": name,
        "status": status,
        "durationMs": int((time.perf_counter() - started) * 1000),
        **({"detail": detail} if detail else {}),
    }
