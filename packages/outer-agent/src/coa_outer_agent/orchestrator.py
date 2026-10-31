"""Outer-agent orchestration across COA, native retrieval, and Portkey."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from .evidence import assemble_context, graph_evidence, knowledge_base_chunk_ids
from .identity import TrustedIdentity
from .mcp_client import McpJsonClient, McpToolError
from .plan_models import SemanticExecutionPlan
from .portkey import PortkeyGenerationClient
from .source_adapters import SourceAdapterRegistry, SourceExecutionError


class UnresolvedSemanticPlanError(ValueError):
    """Raised when COA does not return a resolved, executable plan."""


# ``ExpansionScope`` in serve.smithy.
GRAPH_EXPANSION_SCOPES = frozenset({"SEED_DOCUMENTS", "ENTITLED_DOCUMENTS"})


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
        graph_expansion_scope: str | None = "SEED_DOCUMENTS",
        graph_expansion_max_facts: int = 100,
    ):
        """Configure downstream endpoints and deterministic evidence limits.

        ``graph_expansion_scope`` (ADR 0012) turns the Knowledge Base hits into
        COA graph context: after the sources run, the ``_chunk_id`` of every
        ``MANAGED_KB`` evidence item is handed to COA ``expand_chunks`` and the
        facts it returns join the evidence. ``None`` disables the step.
        """
        self._coa_url = coa_mcp_url
        self._source_adapters = source_adapters
        self._generator = generator
        self._max_evidence_items = max_evidence_items
        self._max_evidence_characters = max_evidence_characters
        self._mcp_client_factory = mcp_client_factory
        if graph_expansion_scope is not None and graph_expansion_scope not in GRAPH_EXPANSION_SCOPES:
            raise ValueError(f"graph_expansion_scope must be one of {sorted(GRAPH_EXPANSION_SCOPES)} or None")
        self._graph_expansion_scope = graph_expansion_scope
        self._graph_expansion_max_facts = graph_expansion_max_facts

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
        trace.append(
            _step(
                "sources.execute",
                source_started,
                "resolved",
                {"items": len(evidence)},
            )
        )

        # ADR 0012: pivot the Knowledge Base hits into COA's graph. The seeds
        # are the chunks the Knowledge Base just showed THIS user; COA verifies
        # that again under the same token and returns only facts from documents
        # the user may read. Enrichment, not a plan directive: when COA cannot
        # expand, the answer is generated from the source evidence alone and
        # the trace says so.
        seeds = knowledge_base_chunk_ids(evidence) if self._graph_expansion_scope else []
        if seeds:
            expand_started = time.perf_counter()
            try:
                raw_expansion = await self._mcp(identity, self._coa_url).call_tool(
                    "expand_chunks",
                    {
                        "chunkIds": seeds,
                        "namespaceId": namespace,
                        "scope": self._graph_expansion_scope,
                        "maxFacts": self._graph_expansion_max_facts,
                    },
                )
            except McpToolError as exc:
                trace.append(_step("coa.expand_chunks", expand_started, "unavailable", {"error": str(exc)[:200]}))
            else:
                expansion = _find_mapping_with(raw_expansion, "facts") or {}
                graph_items = graph_evidence(expansion)
                evidence.extend(graph_items)
                withheld = expansion.get("withheld")
                related = expansion.get("relatedChunks")
                trace.append(
                    _step(
                        "coa.expand_chunks",
                        expand_started,
                        "resolved",
                        {
                            "seeds": len(seeds),
                            "scope": self._graph_expansion_scope,
                            "facts": len(graph_items),
                            "relatedChunks": len(related) if isinstance(related, list) else 0,
                            "withheld": withheld if isinstance(withheld, dict) else {},
                        },
                    )
                )

        evidence = [replace(item, evidence_id=f"E{index}") for index, item in enumerate(evidence, start=1)]
        context, selected = assemble_context(
            evidence,
            max_items=self._max_evidence_items,
            max_characters=self._max_evidence_characters,
        )
        trace.append(
            _step("evidence.assemble", source_started, "resolved", {"items": len(evidence), "selected": len(selected)})
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


def _find_mapping_with(value: Any, key: str) -> dict[str, Any] | None:
    """The first dict (searching depth-first, parsing JSON strings) that carries ``key``."""
    if isinstance(value, str):
        try:
            return _find_mapping_with(json.loads(value), key)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        if key in value:
            return value
        for nested in value.values():
            found = _find_mapping_with(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_mapping_with(nested, key)
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
