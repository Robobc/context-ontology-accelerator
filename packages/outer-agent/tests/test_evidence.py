import pytest
from coa_outer_agent.evidence import (
    assemble_context,
    extract_evidence,
    grounded_query,
    semantic_concepts,
)

pytestmark = pytest.mark.unit


def test_extracts_deduplicated_evidence_from_agentic_result():
    item = {
        "content": {"text": "Project governance evidence."},
        "sourceRetriever": {"identifier": "retriever-1"},
        "metadata": {"x-amz-bedrock-kb-source-uri": "s3://docs/a.pdf"},
    }
    payload = {
        "result": {
            "results": [item, item],
        }
    }
    evidence = extract_evidence(payload)
    assert len(evidence) == 1
    assert evidence[0].evidence_id == "E1"
    assert evidence[0].source == "s3://docs/a.pdf"
    assert evidence[0].source_retriever == "retriever-1"


def test_extracts_nested_serialized_result_and_location():
    payload = (
        '{"event":{"result":{"results":[{"content":{"text":"text"},'
        '"location":{"type":"S3","s3Location":{"uri":"s3://b/key"}},'
        '"score":0.8}]}}}'
    )
    evidence = extract_evidence(payload)
    assert evidence[0].source == "s3://b/key"
    assert evidence[0].score == 0.8


def test_strips_hidden_unicode_from_retrieved_evidence():
    evidence = extract_evidence(
        {"results": [{"content": {"text": "approved\U000e0001 evidence"}}]},
        target_id="domain-kb",
        target_kind="MANAGED_KB",
    )
    assert evidence[0].text == "approved evidence"
    assert evidence[0].target_id == "domain-kb"


def test_context_assembly_enforces_item_and_character_limits():
    evidence = extract_evidence(
        {
            "results": [
                {"content": {"text": "a" * 20}},
                {"content": {"text": "b" * 20}},
            ]
        }
    )
    context, selected = assemble_context(evidence, max_items=2, max_characters=35)
    assert len(context) == 35
    assert [item.evidence_id for item in selected] == ["E1"]


def test_semantic_concepts_ground_the_original_question():
    sparql = "SELECT ?x WHERE { ?x a <urn:test#GovernanceDocument>; ex:resolvesToProject ?p . }"
    concepts = semantic_concepts(sparql)
    assert concepts == ["GovernanceDocument", "resolvesToProject"]
    assert grounded_query("Which project?", concepts) == (
        "Which project?\n\nOntology-grounded concepts and relations: GovernanceDocument, resolvesToProject"
    )


def test_grounded_query_is_original_when_plan_has_no_concepts():
    assert grounded_query("Question", []) == "Question"
