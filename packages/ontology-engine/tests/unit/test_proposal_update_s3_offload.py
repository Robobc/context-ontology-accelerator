# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Manual proposal-edit path must offload Turtle to S3, not write it inline.

Regression guard for the DynamoDB 400 KB item-size class of bug: when a user
edits a proposal's ``ontology_turtle``/``r2rml_turtle`` directly,
``proposals.update_proposal`` must store the bodies in S3 and persist only the
S3 key on the item (``ontology_s3_key``/``r2rml_s3_key``). Writing the full
Turtle inline 500'd once ontologies grew past 400 KB — the same failure the
grounding-override path hit.
"""

from __future__ import annotations

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

# A tiny but valid Turtle doc so the rdflib parse-validation in the handler
# passes (invalid Turtle raises 422 before we reach the persistence path).
_VALID_TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.org/o> a owl:Ontology .
"""
_VALID_R2RML = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <https://example.org/> .
ex:Map a rr:TriplesMap ;
  rr:logicalTable [ rr:tableName "records" ] ;
  rr:subjectMap [ rr:class ex:Record ; rr:template "records/{id}" ] .
"""


def test_manual_turtle_edit_offloads_to_s3(monkeypatch):
    from coa_ontology import proposals

    # Stub the store: a pending proposal exists; capture put_artifact + update.
    put_calls: list[tuple] = []
    update_kwargs: dict = {}

    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            put_calls.append((artifact, content)) or f"proposals/{namespace}/{pid}/latest/{artifact}.ttl"
        ),
    )

    def _capture_update(proposal_id, namespace="default", **fields):
        update_kwargs.update(fields)

    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", _capture_update)

    body = proposals.UpdateProposalRequest(
        proposal_id="p-edit",
        ontology_turtle=_VALID_TTL,
        r2rml_turtle=_VALID_R2RML,
    )
    resp = proposals.update_proposal(body, namespace="ns")

    assert resp == {"proposal_id": "p-edit", "status": "updated"}
    # Both Turtle bodies were written to S3.
    artifacts = {a for a, _ in put_calls}
    assert artifacts == {"ontology", "r2rml"}
    # The DynamoDB write carries S3 KEYS, never the inline Turtle bodies.
    assert "ontology_s3_key" in update_kwargs
    assert "r2rml_s3_key" in update_kwargs
    assert "ontology_turtle" not in update_kwargs
    assert "r2rml_turtle" not in update_kwargs
    assert update_kwargs["status"] == "updated"


def test_manual_edit_no_longer_canonicalizes_datatypes(monkeypatch):
    """Editing a proposal persists the user's Turtle VERBATIM — the PATCH path
    must NOT silently rewrite malformed datatype tokens anymore. (Supersedes the
    old auto-canonicalize behavior; the DatatypeTokenValidator + consent-gated
    /repair-datatypes flow replaces it.) A lowercase xsd:datetime must survive
    unchanged in the stored artifact so the validator can flag it.
    """
    from coa_ontology import proposals

    malformed_ttl = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<https://example.org/p> a owl:DatatypeProperty ; rdfs:range xsd:datetime .
"""
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            stored.update({artifact: content}) or f"proposals/{namespace}/{pid}/latest/{artifact}.ttl"
        ),
    )
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", lambda pid, namespace="default", **f: None)

    body = proposals.UpdateProposalRequest(proposal_id="p-verbatim", ontology_turtle=malformed_ttl)
    proposals.update_proposal(body, namespace="ns")

    # The malformed token is stored VERBATIM — not rewritten to xsd:dateTime.
    assert stored["ontology"] == malformed_ttl
    assert "xsd:datetime" in stored["ontology"]


def test_invalid_turtle_rejected_before_persistence(monkeypatch):
    from coa_ontology import proposals
    from fastapi import HTTPException

    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    # If we reach persistence, fail loudly — invalid Turtle must 422 first.
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda *a, **k: pytest.fail("should not persist invalid Turtle"),
    )

    body = proposals.UpdateProposalRequest(proposal_id="p-bad", ontology_turtle="this is not turtle {{{")
    with pytest.raises(HTTPException) as exc:
        proposals.update_proposal(body, namespace="ns")
    assert exc.value.status_code == 422


def test_parseable_ontology_turtle_is_rejected_as_r2rml(monkeypatch):
    """A presigned upload must not cross ontology bytes into the mapping slot."""
    from coa_ontology import proposals
    from fastapi import HTTPException

    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda *a, **k: pytest.fail("should not persist non-R2RML Turtle as a mapping"),
    )

    body = proposals.UpdateProposalRequest(proposal_id="p-crossed", r2rml_turtle=_VALID_TTL)
    with pytest.raises(HTTPException) as exc:
        proposals.update_proposal(body, namespace="ns")

    assert exc.value.status_code == 422
    assert "does not define any rr:TriplesMap" in exc.value.detail


def test_unstructured_grounding_override_patches_turtle(monkeypatch):
    """Grounding override on an UNSTRUCTURED proposal patches the stored Turtle
    directly (no DB-catalog rebuild) and persists via S3 offload."""
    from coa_ontology import proposals

    cls = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#FinancialInstrument"
    old_target = "http://fibo/Agreement"
    new_target = "http://fibo/Commitment"
    induced_ttl = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{cls}> a owl:Class ; rdfs:label "FinancialInstrument" ;
    rdfs:subClassOf <{old_target}> ; skos:closeMatch <{old_target}> .
"""
    # A pending UNSTRUCTURED proposal whose metadata carries the match with
    # both candidates, so the override can re-pick.
    item = {
        "proposal_id": "p-uns",
        "status": "pending",
        "source_type": "UNSTRUCTURED",
        "ontology_id": f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#",
        "metadata": {
            "matches": [
                {
                    "source_table": "FinancialInstrument",
                    "source_column": "",
                    "matched_class_uri": old_target,
                    "matched_ontology_id": "fibo",
                    "similarity": 0.9,
                    "match_type": "high_confidence",
                    "candidates": [
                        {"entity_uri": old_target, "ontology_id": "fibo", "lexical_sim": 0.9},
                        {"entity_uri": new_target, "ontology_id": "fibo", "lexical_sim": 0.85},
                    ],
                }
            ]
        },
    }
    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", lambda pid, namespace="default": item)
    monkeypatch.setattr(
        proposals.dynamo_store, "_get_artifact_s3", lambda namespace, pid, artifact, version="latest": induced_ttl
    )

    put_artifacts: dict[str, str] = {}
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            put_artifacts.update({artifact: content}) or f"proposals/{namespace}/{pid}/latest/{artifact}.ttl"
        ),
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "put_proposal_matches_s3",
        lambda pid, matches, namespace="default": f"proposals/{namespace}/{pid}/latest/matches.json",
    )
    update_kwargs: dict = {}
    monkeypatch.setattr(
        proposals.dynamo_store,
        "update_proposal",
        lambda pid, namespace="default", **f: update_kwargs.update(f),
    )

    body = proposals.UpdateProposalRequest(
        proposal_id="p-uns",
        grounding_overrides={"FinancialInstrument": new_target},
    )
    resp = proposals.update_proposal(body, namespace="ns")

    assert resp == {"proposal_id": "p-uns", "status": "updated"}
    # The patched ontology Turtle was written to S3 (no r2rml for unstructured).
    assert "ontology" in put_artifacts
    assert "r2rml" not in put_artifacts
    patched = put_artifacts["ontology"]
    # Override applied: new target grounded, old target gone.
    assert new_target in patched
    assert old_target not in patched
    # Persisted keys, not inline bodies.
    assert update_kwargs["status"] == "updated"
    assert "ontology_s3_key" in update_kwargs
    assert "matches_s3_key" in update_kwargs
