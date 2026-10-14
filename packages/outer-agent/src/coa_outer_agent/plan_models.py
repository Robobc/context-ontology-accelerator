# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict consumer-side validation for the semantic plan contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Reject fields not declared by the semantic plan contract."""

    model_config = ConfigDict(extra="forbid")


class Confidence(StrictModel):
    """Bounded confidence supplied by semantic resolution."""

    score: float = Field(ge=0.0, le=1.0)
    rationale: str | None = None


class ResolvedConcept(StrictModel):
    """Ontology concept selected by COA semantic grounding."""

    iri: str
    label: str | None = None
    confidence: Confidence | None = None


class CanonicalEntity(StrictModel):
    """Canonical enterprise identity with confidence and provenance."""

    entityType: str
    canonicalId: str
    label: str | None = None
    confidence: Confidence | None = None
    provenance: list[str] = Field(default_factory=list)
    freshness: str | None = None


class Ambiguity(StrictModel):
    """Semantic choice requiring clarification before execution."""

    code: str
    message: str
    candidates: list[str] = Field(default_factory=list)


class SourceDirective(StrictModel):
    """One validated instruction for a logical source target."""

    targetId: str
    targetKind: Literal["MANAGED_KB", "GOVERNED_QUERY", "EXTERNAL_GRAPH", "COA_GRAPH"]
    language: Literal["NATURAL_LANGUAGE", "SEMANTIC_INTENT", "SPARQL"]
    query: str = Field(min_length=1, max_length=100_000)
    readOnly: bool
    requiresCompilation: bool
    scopeRef: str | None = None
    expectedEvidence: list[Literal["DOCUMENT", "CITATION", "ROW", "GRAPH_BINDING", "PROVENANCE"]] = Field(
        default_factory=list, max_length=8
    )

    @model_validator(mode="after")
    def require_read_only(self):
        """Reject any plan that could mutate a source."""
        if not self.readOnly:
            raise ValueError("semantic source directives must be read-only")
        return self


class SemanticExecutionPlan(StrictModel):
    """Validated source-neutral semantic execution plan."""

    planVersion: Literal["1.0"]
    status: Literal["RESOLVED", "AMBIGUOUS", "UNSUPPORTED"]
    namespaceId: str
    profileMode: Literal["ACCELERATE", "ADOPT", "FEDERATE"]
    profileVersion: str
    originalQuestion: str
    groundedQuestion: str
    ontologyVersion: str
    resolvedConcepts: list[ResolvedConcept] = Field(default_factory=list, max_length=32)
    canonicalEntities: list[CanonicalEntity] = Field(default_factory=list, max_length=100)
    ambiguities: list[Ambiguity] = Field(default_factory=list, max_length=100)
    sourceDirectives: list[SourceDirective] = Field(default_factory=list, max_length=8)
    expectedEvidence: list[Literal["DOCUMENT", "CITATION", "ROW", "GRAPH_BINDING", "PROVENANCE"]] = Field(
        default_factory=list, max_length=8
    )
    generationTarget: str | None = None

    @model_validator(mode="after")
    def resolved_plan_has_directives(self):
        """Require directives only when semantic resolution is complete."""
        if self.status == "RESOLVED" and not self.sourceDirectives:
            raise ValueError("resolved semantic plan requires source directives")
        if self.status != "RESOLVED" and self.sourceDirectives:
            raise ValueError("unresolved semantic plan must not emit source directives")
        return self
