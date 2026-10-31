import pytest
from coa_outer_agent.evidence import (
    GRAPH_EXPANSION_MAX_SEEDS,
    GRAPH_TARGET_ID,
    GRAPH_TARGET_KIND,
    Evidence,
    assemble_context,
    extract_evidence,
    graph_evidence,
    grounded_query,
    knowledge_base_chunk_ids,
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


# ── ADR 0012: Knowledge Base hits -> COA graph expansion ─────────────────────


def _kb_item(evidence_id: str, metadata: dict, kind: str = "MANAGED_KB") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        target_id="domain-kb",
        target_kind=kind,
        text="chunk text",
        source="sharepoint://doc",
        score=0.5,
        source_retriever="kb",
        metadata=metadata,
    )


def test_chunk_ids_are_taken_from_managed_kb_hits_in_rank_order_without_duplicates():
    items = [
        _kb_item("E1", {"_chunk_id": "Ukhx_fin"}),
        _kb_item("E2", {"x-amz-bedrock-kb-chunk-id": "Ukhx_hr"}),
        _kb_item("E3", {"_chunk_id": "Ukhx_fin"}),  # same chunk again
        _kb_item("E4", {"chunk_id": "Ukhx_shared"}),
        _kb_item("E5", {}),  # no id: nothing to expand from
        _kb_item("E6", {"_chunk_id": "not a graph id!"}),  # outside the KnowledgeBaseChunkId grammar
        _kb_item("E7", {"_chunk_id": "Ukhx_other"}, kind="COA_GRAPH"),  # not a Knowledge Base hit
    ]
    assert knowledge_base_chunk_ids(items) == ["Ukhx_fin", "Ukhx_hr", "Ukhx_shared"]


def test_chunk_ids_are_cut_to_one_expand_call():
    items = [_kb_item(f"E{i}", {"_chunk_id": f"c{i}"}) for i in range(80)]
    assert len(knowledge_base_chunk_ids(items)) == GRAPH_EXPANSION_MAX_SEEDS


def test_graph_evidence_is_one_item_per_fact_with_document_provenance():
    expansion = {
        "entities": [
            {"id": "e:atlas", "label": "Project Atlas", "class": "Project"},
            {"id": "e:basel", "label": "R&D Basel", "class": "Department"},
        ],
        "facts": [
            {
                "id": "f1",
                "text": "Project Atlas HAS BUDGET\U000e0001 1.2M EUR",
                "subjectId": "e:atlas",
                "chunkId": "Ukhx_fin",
                "sourceDocumentId": "s3://hub/finance/budget.md",
            },
            {
                "id": "f2",
                "text": "Project Atlas CHARGED TO R&D Basel",
                "subjectId": "e:atlas",
                "objectId": "e:basel",
                "chunkId": "Ukhx_fin",
                "sourceDocumentId": "s3://hub/finance/budget.md",
            },
            {"id": "f1", "text": "duplicate", "subjectId": "e:atlas", "chunkId": "x", "sourceDocumentId": "y"},
            {"id": "f3", "text": "   ", "subjectId": "e:atlas", "chunkId": "x", "sourceDocumentId": "y"},
            "not a fact",
        ],
        "relatedChunks": [],
        "withheld": {"seedChunks": 0, "facts": 1, "relatedChunks": 1},
    }
    items = graph_evidence(expansion)
    assert [i.evidence_id for i in items] == ["G1", "G2"]
    assert all(i.target_id == GRAPH_TARGET_ID and i.target_kind == GRAPH_TARGET_KIND for i in items)
    assert items[0].text == "Project Atlas HAS BUDGET 1.2M EUR"  # hidden character stripped
    assert items[0].source == "s3://hub/finance/budget.md"
    assert items[0].metadata["subjectLabel"] == "Project Atlas" and items[0].metadata["objectLabel"] is None
    assert items[1].metadata["objectLabel"] == "R&D Basel"
    assert items[1].source_retriever == "expand_chunks"


@pytest.mark.parametrize("payload", [None, "not json", "[]", {"facts": "nope"}, {"entities": []}, 42])
def test_graph_evidence_is_empty_for_anything_but_a_facts_list(payload):
    assert graph_evidence(payload) == []


def test_graph_evidence_accepts_the_serialized_tool_result():
    serialized = '{"entities": [], "facts": [{"id": "f1", "text": "A B C", "subjectId": "s", "chunkId": "c"}]}'
    items = graph_evidence(serialized)
    assert len(items) == 1 and items[0].source == GRAPH_TARGET_ID  # no document named: falls back to the target
