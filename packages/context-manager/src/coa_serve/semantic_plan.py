# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compile ontology translation into a source-neutral execution plan."""

from __future__ import annotations

import re
from typing import Any

from coa_common import strip_hidden_model_characters
from coa_common.constants import MAX_QUERY_CODEPOINTS

_ABSOLUTE_IRI = re.compile(r"<((?:https?|urn):[^>\s]+)>")
_BUILTIN_IRI_PREFIXES = (
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/2001/XMLSchema#",
    "http://www.w3.org/2002/07/owl#",
)
_MAX_CONCEPTS = 32
_MAX_DIRECTIVE_QUERY = 100_000

_EXPECTED_EVIDENCE = {
    "MANAGED_KB": ["DOCUMENT", "CITATION", "PROVENANCE"],
    "GOVERNED_QUERY": ["ROW", "PROVENANCE"],
    "EXTERNAL_GRAPH": ["GRAPH_BINDING", "PROVENANCE"],
    "COA_GRAPH": ["GRAPH_BINDING", "PROVENANCE"],
}


def build_semantic_execution_plan(
    *,
    question: str,
    namespace_id: str,
    integration_profile: dict[str, Any],
    sparql: str,
    confidence: float,
) -> dict[str, Any]:
    """Build a plan containing instructions, never source evidence or grants."""
    # @amsec-loaf: model-validation
    original = strip_hidden_model_characters(question)
    safe_sparql = strip_hidden_model_characters(sparql)
    if len(safe_sparql) > _MAX_DIRECTIVE_QUERY:
        raise ValueError("semantic directive exceeds the plan contract limit")

    concepts = _resolved_concepts(safe_sparql, confidence)
    grounded = _grounded_question(original, concepts)
    configured_directives = [
        _directive(target, grounded=grounded, sparql=safe_sparql) for target in integration_profile["sourceTargets"]
    ]
    legacy_unversioned = integration_profile["ontologyVersion"] == "legacy-unversioned"
    directives = [] if legacy_unversioned else configured_directives
    expected_evidence = list(
        dict.fromkeys(evidence for directive in directives for evidence in directive["expectedEvidence"])
    )

    return {
        "planVersion": "1.0",
        "status": "AMBIGUOUS" if legacy_unversioned else "RESOLVED",
        "namespaceId": namespace_id,
        "profileMode": integration_profile["mode"],
        "profileVersion": integration_profile["profileVersion"],
        "originalQuestion": original,
        "groundedQuestion": grounded,
        "ontologyVersion": integration_profile["ontologyVersion"],
        "resolvedConcepts": concepts,
        # Entity linking is a separate governed capability. Never fabricate a
        # canonical entity from labels found in generated query text.
        "canonicalEntities": [],
        "ambiguities": (
            [
                {
                    "code": "ONTOLOGY_VERSION_UNVERIFIED",
                    "message": (
                        "Bind the namespace integration profile to an approved "
                        "ontology version before executing this plan."
                    ),
                    "candidates": [],
                }
            ]
            if legacy_unversioned
            else []
        ),
        "sourceDirectives": directives,
        "expectedEvidence": expected_evidence,
        **(
            {"generationTarget": integration_profile["generationTarget"]}
            if integration_profile.get("generationTarget")
            else {}
        ),
    }


def _resolved_concepts(sparql: str, confidence: float) -> list[dict[str, Any]]:
    seen: set[str] = set()
    concepts: list[dict[str, Any]] = []
    score = max(0.0, min(float(confidence), 1.0))
    for iri in _ABSOLUTE_IRI.findall(sparql):
        if iri.startswith(_BUILTIN_IRI_PREFIXES) or iri in seen:
            continue
        seen.add(iri)
        label = re.split(r"[/#:]", iri.rstrip("/#"))[-1]
        concepts.append(
            {
                "iri": iri,
                "label": label,
                "confidence": {
                    "score": score,
                    "rationale": "Resolved from the ontology-grounded SPARQL translation.",
                },
            }
        )
        if len(concepts) >= _MAX_CONCEPTS:
            break
    return concepts


def _grounded_question(question: str, concepts: list[dict[str, Any]]) -> str:
    labels = [concept["label"] for concept in concepts if concept.get("label")]
    if not labels:
        return question
    suffix = "\n\nResolved ontology concepts: " + ", ".join(labels)
    available = MAX_QUERY_CODEPOINTS - len(question)
    if available <= 0:
        return question
    return question + suffix[:available]


def _directive(
    target: dict[str, str],
    *,
    grounded: str,
    sparql: str,
) -> dict[str, Any]:
    kind = target["kind"]
    if kind == "MANAGED_KB":
        language = "NATURAL_LANGUAGE"
        query = grounded
        requires_compilation = False
    elif kind == "GOVERNED_QUERY":
        # The source-owned read-only tool compiles and validates the semantic
        # intent against its approved objects. COA does not emit executable
        # source queries until that adapter contract is available.
        language = "SEMANTIC_INTENT"
        query = grounded
        requires_compilation = True
    else:
        language = "SPARQL"
        query = sparql
        requires_compilation = False

    return {
        "targetId": target["targetId"],
        "targetKind": kind,
        "language": language,
        "query": query,
        "readOnly": True,
        "requiresCompilation": requires_compilation,
        **({"scopeRef": target["scopeRef"]} if target.get("scopeRef") else {}),
        "expectedEvidence": list(_EXPECTED_EVIDENCE[kind]),
    }
