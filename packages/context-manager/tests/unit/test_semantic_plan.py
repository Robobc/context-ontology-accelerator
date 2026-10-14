# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from coa_serve.semantic_plan import build_semantic_execution_plan

pytestmark = pytest.mark.unit


def _profile() -> dict:
    return {
        "mode": "ADOPT",
        "profileVersion": "domain-v1",
        "ontologyVersion": "ontology-2026-10-14",
        "generationTarget": "portkey",
        "sourceTargets": [
            {"targetId": "domain-kb", "kind": "MANAGED_KB", "accessMode": "READ_ONLY"},
            {"targetId": "analytics-query", "kind": "GOVERNED_QUERY", "accessMode": "READ_ONLY"},
            {
                "targetId": "domain-graph",
                "kind": "COA_GRAPH",
                "accessMode": "READ_ONLY",
                "scopeRef": "domain-graph-scope",
            },
        ],
    }


def test_builds_profile_specific_read_only_directives():
    plan = build_semantic_execution_plan(
        question="Trace project P-123",
        namespace_id="123e4567-e89b-42d3-a456-426614174000",
        integration_profile=_profile(),
        sparql="SELECT ?p WHERE { ?p a <https://example.com/ontology/Project> }",
        confidence=0.9,
    )

    assert plan["planVersion"] == "1.0"
    assert plan["profileMode"] == "ADOPT"
    assert plan["canonicalEntities"] == []
    assert {directive["targetKind"] for directive in plan["sourceDirectives"]} == {
        "MANAGED_KB",
        "GOVERNED_QUERY",
        "COA_GRAPH",
    }
    assert all(directive["readOnly"] is True for directive in plan["sourceDirectives"])
    query = next(d for d in plan["sourceDirectives"] if d["targetKind"] == "GOVERNED_QUERY")
    assert query["language"] == "SEMANTIC_INTENT"
    assert query["requiresCompilation"] is True
    graph = next(d for d in plan["sourceDirectives"] if d["targetKind"] == "COA_GRAPH")
    assert graph["query"].startswith("SELECT")
    assert graph["scopeRef"] == "domain-graph-scope"


def test_strips_hidden_unicode_from_question_and_translation():
    plan = build_semantic_execution_plan(
        question="Domain\U000e0001",
        namespace_id="123e4567-e89b-42d3-a456-426614174000",
        integration_profile=_profile(),
        sparql="SELECT * WHERE { ?s a <https://example.com/Contract\U000e0001> }",
        confidence=2.0,
    )
    assert "\U000e0001" not in str(plan)
    assert plan["resolvedConcepts"][0]["confidence"]["score"] == 1.0


def test_never_truncates_the_original_question_for_grounding():
    question = "q" * 4000
    plan = build_semantic_execution_plan(
        question=question,
        namespace_id="123e4567-e89b-42d3-a456-426614174000",
        integration_profile=_profile(),
        sparql="SELECT * WHERE { ?s a <https://example.com/Project> }",
        confidence=0.5,
    )
    assert plan["originalQuestion"] == question
    assert plan["groundedQuestion"] == question


def test_legacy_unversioned_profile_is_non_executable():
    profile = _profile()
    profile["ontologyVersion"] = "legacy-unversioned"
    plan = build_semantic_execution_plan(
        question="Trace project P-123",
        namespace_id="123e4567-e89b-42d3-a456-426614174000",
        integration_profile=profile,
        sparql="SELECT ?p WHERE { ?p a <https://example.com/ontology/Project> }",
        confidence=0.9,
    )
    assert plan["status"] == "AMBIGUOUS"
    assert plan["sourceDirectives"] == []
    assert plan["expectedEvidence"] == []
    assert plan["ambiguities"][0]["code"] == "ONTOLOGY_VERSION_UNVERIFIED"
