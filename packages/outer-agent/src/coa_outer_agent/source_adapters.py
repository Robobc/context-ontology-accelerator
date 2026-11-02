# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Source-native execution adapters selected only by semantic-plan target IDs."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .evidence import Evidence, extract_evidence
from .identity import TrustedIdentity
from .plan_models import SourceDirective


class SourceExecutionError(RuntimeError):
    """Raised when a required source target cannot return governed evidence."""


@dataclass(frozen=True)
class SourceTargetConfig:
    """Deployment-owned configuration for one logical plan target.

    ``region`` is the Region the Knowledge Bases live in; ``None`` means the
    runtime's own Region. ``data_source_ids`` restricts every retriever of the
    target to those Knowledge Base data sources (the same ``_data_source_id``
    scope a COA-registered ``BEDROCK_MANAGED_KB`` source carries); empty means
    the whole Knowledge Base.
    """

    target_id: str
    kind: str
    knowledge_base_ids: tuple[str, ...] = ()
    max_agent_iterations: int = 5
    region: str | None = None
    data_source_ids: tuple[str, ...] = ()
    # Bedrock rejects MANAGED reranking for Knowledge Bases on a custom
    # embedding model (managed-search KBs, FMKB); NONE lets those targets run.
    reranking: str = "MANAGED"


class SourceAdapter(Protocol):
    """Pluggable executor for one generic integration target kind."""

    async def execute(
        self,
        directive: SourceDirective,
        target: SourceTargetConfig,
        identity: TrustedIdentity,
    ) -> list[Evidence]:
        """Execute one validated read-only directive and return evidence."""
        ...


class SourceAdapterRegistry:
    """Bind logical plan targets to administrator-configured source adapters."""

    def __init__(
        self,
        *,
        region: str,
        targets: dict[str, SourceTargetConfig],
        managed_kb_client: Any | None = None,
        adapters: dict[str, SourceAdapter] | None = None,
    ) -> None:
        """Create adapters for the administrator-owned logical targets."""
        self._targets = targets
        if adapters and "MANAGED_KB" in adapters:
            raise ValueError("MANAGED_KB is a reserved built-in adapter kind")
        managed_kb = ManagedKnowledgeBaseAdapter(
            region=region,
            client=managed_kb_client,
        )
        self._adapters: dict[str, SourceAdapter] = {
            "MANAGED_KB": managed_kb,
            **(adapters or {}),
        }

    async def execute(
        self,
        directive: SourceDirective,
        identity: TrustedIdentity,
    ) -> list[Evidence]:
        """Validate a plan target and execute it through the matching adapter."""
        target = self._targets.get(directive.targetId)
        if target is None:
            raise SourceExecutionError(f"Semantic plan target is not configured: {directive.targetId}")
        if target.kind != directive.targetKind:
            raise SourceExecutionError(f"Semantic plan target kind mismatch: {directive.targetId}")
        adapter = self._adapters.get(directive.targetKind)
        if adapter is None:
            # Cross-trust adapters need an explicit OBO/query contract. Never
            # invent credentials or forward a token to an unknown audience.
            raise SourceExecutionError(
                f"No governed adapter is configured for {directive.targetKind} target {directive.targetId}"
            )
        evidence = await adapter.execute(directive, target, identity)
        if not isinstance(evidence, list) or any(not isinstance(item, Evidence) for item in evidence):
            raise SourceExecutionError(f"Adapter returned invalid evidence for target {directive.targetId}")
        if any(item.target_id != target.target_id or item.target_kind != target.kind for item in evidence):
            raise SourceExecutionError(
                f"Adapter returned mismatched evidence provenance for target {directive.targetId}"
            )
        return evidence


class ManagedKnowledgeBaseAdapter:
    """Execute evidence-only AgenticRetrieveStream under trusted user context."""

    def __init__(self, *, region: str, client: Any | None = None) -> None:
        """Configure lazy, per-Region Bedrock Agent Runtime clients.

        ``region`` is the default for targets that name none. An injected
        ``client`` serves every Region (tests).
        """
        self._region = region
        self._clients: dict[str, Any] = {}
        self._injected = client
        self._lock = threading.Lock()

    def _get_client(self, region: str):
        if self._injected is not None:
            return self._injected
        client = self._clients.get(region)
        if client is None:
            with self._lock:
                client = self._clients.get(region)
                if client is None:
                    client = self._clients[region] = boto3.client(
                        "bedrock-agent-runtime",
                        region_name=region,
                        config=BotoConfig(
                            read_timeout=120,
                            connect_timeout=10,
                            retries={"mode": "adaptive", "max_attempts": 3},
                        ),
                    )
        return client

    async def execute(
        self,
        directive: SourceDirective,
        target: SourceTargetConfig,
        identity: TrustedIdentity,
    ) -> list[Evidence]:
        """Run AgenticRetrieveStream off the event loop and return evidence."""
        if not target.knowledge_base_ids:
            raise SourceExecutionError(f"Managed KB target has no configured retrievers: {target.target_id}")
        return await asyncio.to_thread(
            self._execute_sync,
            directive,
            target,
            identity,
        )

    def _execute_sync(
        self,
        directive: SourceDirective,
        target: SourceTargetConfig,
        identity: TrustedIdentity,
    ) -> list[Evidence]:
        # @amsec-loaf: mcp_authentication
        # The validated human identity is preserved as Managed-KB userContext;
        # service credentials authorize the API call but never replace the user.
        retriever_kb: dict[str, Any] = {}
        if target.data_source_ids:
            retriever_kb["retrievalOverrides"] = {
                "filter": {"in": {"key": "_data_source_id", "value": list(target.data_source_ids)}}
            }
        try:
            response = self._get_client(target.region or self._region).agentic_retrieve_stream(
                agenticRetrieveConfiguration={
                    "foundationModelType": "MANAGED",
                    "rerankingModelType": target.reranking,
                    "maxAgentIteration": target.max_agent_iterations,
                },
                generateResponse=False,
                messages=[{"role": "user", "content": {"text": directive.query}}],
                retrievers=[
                    {"configuration": {"knowledgeBase": {"knowledgeBaseId": knowledge_base_id, **retriever_kb}}}
                    for knowledge_base_id in target.knowledge_base_ids
                ],
                userContext=identity.knowledge_user_context,
            )
        except ClientError as exc:
            # A rejected call (ValidationException, AccessDenied, ...) is a
            # source failure the operator can act on, not an internal error.
            # The service message names the parameter; it carries no content.
            error = exc.response.get("Error", {})
            raise SourceExecutionError(
                f"Managed KB call rejected for target {target.target_id}: "
                f"{error.get('Code', 'ClientError')}: {error.get('Message', '')}"[:500]
            ) from exc
        except BotoCoreError as exc:
            raise SourceExecutionError(
                f"Managed KB call failed for target {target.target_id}: {type(exc).__name__}"
            ) from exc

        stream = response.get("stream")
        if stream is None:
            raise SourceExecutionError("Managed KB returned no event stream")
        final: dict[str, Any] | None = None
        try:
            for event in stream:
                result = event.get("result") or event.get("resultEvent")
                if isinstance(result, dict):
                    final = result
                for error_key in (
                    "accessDeniedException",
                    "badGatewayException",
                    "dependencyFailedException",
                    "internalServerException",
                    "resourceNotFoundException",
                    "serviceQuotaExceededException",
                    "throttlingException",
                    "validationException",
                ):
                    if error_key in event:
                        raise SourceExecutionError(f"Managed KB retrieval failed: {error_key}")
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        if final is None or not isinstance(final.get("results"), list):
            raise SourceExecutionError("Managed KB returned no result event")
        if final.get("generatedResponse"):
            raise SourceExecutionError("Managed KB returned generation despite evidence-only mode")
        return extract_evidence(
            {"results": final["results"]},
            target_id=target.target_id,
            target_kind=target.kind,
        )
