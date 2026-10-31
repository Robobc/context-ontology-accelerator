# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fork (ADR 0012): agent-driven lexical-graph expansion from Knowledge Base chunk ids.

The caller's agent retrieved chunks from the Knowledge Base as the user and hands
COA their ``_chunk_id`` values; COA walks the graph two hops out and returns only
what lies in documents the caller may read. The fixture mirrors the ACL case the
feature was verified against on dev: finance is readable by alice, HR by bob,
and the two documents share one entity ("Project Atlas") so a seed in finance
reaches a fact in HR through the graph.
"""

from __future__ import annotations

import pytest
from coa_common.constants import to_graphrag_tenant_id
from coa_common.kb_ids import stored_chunk_id
from coa_serve.request_identity import reset_request_user, set_request_user
from coa_serve.tier3.chunk_expander import (
    RELATED_FACTS,
    SCOPE_ENTITLED_DOCUMENTS,
    SCOPE_SEED_DOCUMENTS,
    SEED_FACTS,
    SEED_PROVENANCE,
    ChunkExpander,
)
from coa_serve.tier3.document_entitlement import DocumentEntitlementGate

pytestmark = pytest.mark.unit

NS = "0f1e2d3c-4b5a-4697-8899-aabbccddeeff"
TENANT = to_graphrag_tenant_id(NS)
FINANCE = "s3://hub/finance/budget-2026.md"
HR = "s3://hub/hr/org-2026.md"
ACL = {"alice@example.com": {FINANCE}, "bob@example.com": {HR}, "carol@example.com": set()}

FIN_KB, HR_KB = "Ukhx_fin", "Ukhx_hr"
FIN_CHUNK, HR_CHUNK = stored_chunk_id(TENANT, FIN_KB), stored_chunk_id(TENANT, HR_KB)
ATLAS, BASEL, ROSSI = "e:atlas", "e:basel", "e:rossi"
F_BUDGET, F_CHARGED, F_MANAGED = "f:budget", "f:charged", "f:managed"

# Graph fixture rows, keyed by the Cypher template they answer.
PROVENANCE = {FIN_CHUNK: FINANCE, HR_CHUNK: HR}


def _fact_row(chunk, fact_id, text, subj, subj_label, subj_class, obj=None, obj_label=None, obj_class=None, **extra):
    return {
        "chunkId": chunk,
        "factId": fact_id,
        "fact": text,
        "subjectId": subj,
        "subjectLabel": subj_label,
        "subjectClass": subj_class,
        "objectId": obj,
        "objectLabel": obj_label,
        "objectClass": obj_class,
        **extra,
    }


SEED_ROWS = {
    FIN_CHUNK: [
        _fact_row(FIN_CHUNK, F_BUDGET, "Project Atlas HAS BUDGET 1.2M EUR", ATLAS, "Project Atlas", "Project"),
        _fact_row(
            FIN_CHUNK,
            F_CHARGED,
            "Project Atlas CHARGED TO R&D Basel",
            ATLAS,
            "Project Atlas",
            "Project",
            BASEL,
            "R&D Basel",
            "Department",
        ),
    ],
    HR_CHUNK: [
        _fact_row(
            HR_CHUNK,
            F_MANAGED,
            "Project Atlas MANAGED BY M. Rossi",
            ATLAS,
            "Project Atlas",
            "Project",
            ROSSI,
            "M. Rossi",
            "Person",
        ),
    ],
}
# Hop 2: every fact mentioning an entity, in the OTHER chunk.
RELATED_ROWS = {
    ATLAS: [
        _fact_row(
            HR_CHUNK,
            F_MANAGED,
            "Project Atlas MANAGED BY M. Rossi",
            ATLAS,
            "Project Atlas",
            "Project",
            ROSSI,
            "M. Rossi",
            "Person",
            viaEntityId=ATLAS,
            uri=HR,
        ),
        _fact_row(
            FIN_CHUNK,
            F_BUDGET,
            "Project Atlas HAS BUDGET 1.2M EUR",
            ATLAS,
            "Project Atlas",
            "Project",
            viaEntityId=ATLAS,
            uri=FINANCE,
        ),
        _fact_row(
            FIN_CHUNK,
            F_CHARGED,
            "Project Atlas CHARGED TO R&D Basel",
            ATLAS,
            "Project Atlas",
            "Project",
            BASEL,
            "R&D Basel",
            "Department",
            viaEntityId=ATLAS,
            uri=FINANCE,
        ),
    ],
}


class FakeStore:
    """Answers the three expander templates from the fixture; records every query."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def execute_query_with_retry(self, cypher, params):
        self.queries.append((cypher, params))
        if "RETURN id(c) AS chunkId, s.source_uri AS uri" in cypher:
            return [{"chunkId": c, "uri": PROVENANCE[c]} for c in params["chunk_ids"] if c in PROVENANCE]
        if "viaEntityId" in cypher:
            seeds = set(params["seed_chunk_ids"])
            return [r for e in params["entity_ids"] for r in RELATED_ROWS.get(e, []) if r["chunkId"] not in seeds]
        if "WHERE id(c) IN $chunk_ids" in cypher:
            return [r for c in params["chunk_ids"] for r in SEED_ROWS.get(c, [])]
        raise AssertionError(f"unexpected cypher: {cypher}")


class AclProbe:
    """Stands in for the per-user Knowledge Base retriever: answers entitlement per caller."""

    enforces_user_context = True

    def __init__(self):
        self.probed: list[set[str]] = []

    async def probe_source_uris(self, source_uris, user_id=None):
        asked = {u for u in source_uris if u}
        self.probed.append(asked)
        allowed = ACL.get(user_id or "", set())
        return {u: u in allowed for u in asked}


def _expander(applies: bool = True) -> tuple[ChunkExpander, FakeStore, AclProbe]:
    probe = AclProbe()
    gate = DocumentEntitlementGate("neptune-db://h:8182", lambda ns: probe if applies else None, cache_ttl_s=60)
    store = FakeStore()
    gate._get_store = lambda: store  # type: ignore[method-assign]
    return ChunkExpander(gate), store, probe


@pytest.fixture()
def alice():
    token = set_request_user("alice@example.com")
    yield "alice@example.com"
    reset_request_user(token)


class TestSeedVerification:
    """The caller is never trusted about which chunks it retrieved."""

    @pytest.mark.asyncio
    async def test_seed_in_a_document_the_caller_may_not_read_is_not_expanded(self, alice):
        expander, store, probe = _expander()
        result = await expander.expand(NS, [HR_KB])
        assert result.facts == [] and result.entities == [] and result.related_chunks == []
        assert result.withheld_seed_chunks == 1
        # Provenance was resolved and the document probed as alice; the graph was not walked.
        assert probe.probed == [{HR}]
        assert len(store.queries) == 1 and "source_uri" in store.queries[0][0]

    @pytest.mark.asyncio
    async def test_unknown_seed_is_withheld_the_same_way(self, alice):
        expander, _, probe = _expander()
        result = await expander.expand(NS, ["no-such-chunk"])
        assert result.withheld_seed_chunks == 1 and result.facts == []
        assert probe.probed == []  # nothing to probe: the count alone does not say which case it was

    @pytest.mark.asyncio
    async def test_no_caller_means_nothing_expands(self):
        expander, _, _ = _expander()
        result = await expander.expand(NS, [FIN_KB])
        assert result.facts == [] and result.withheld_seed_chunks == 1

    @pytest.mark.asyncio
    async def test_duplicate_seeds_count_once(self, alice):
        expander, _, _ = _expander()
        result = await expander.expand(NS, [FIN_KB, FIN_KB])
        assert result.withheld_seed_chunks == 0
        assert {f.id for f in result.facts} == {F_BUDGET, F_CHARGED}


class TestExpansion:
    @pytest.mark.asyncio
    async def test_seed_facts_and_entities_carry_knowledge_base_ids(self, alice):
        expander, _, _ = _expander()
        result = await expander.expand(NS, [FIN_KB])
        budget = next(f for f in result.facts if f.id == F_BUDGET)
        assert budget.chunk_id == FIN_KB  # the KB _chunk_id, not the graph node id
        assert budget.source_document_id == FINANCE
        assert budget.subject_id == ATLAS and budget.object_id is None
        charged = next(f for f in result.facts if f.id == F_CHARGED)
        assert charged.object_id == BASEL
        assert {(e.id, e.label, e.class_) for e in result.entities} == {
            (ATLAS, "Project Atlas", "Project"),
            (BASEL, "R&D Basel", "Department"),
        }

    @pytest.mark.asyncio
    async def test_seed_documents_scope_withholds_the_other_document(self, alice):
        """Alice seeds from finance; the HR fact reached through 'Project Atlas' is counted, not shown."""
        expander, _, probe = _expander()
        result = await expander.expand(NS, [FIN_KB], scope=SCOPE_SEED_DOCUMENTS)
        assert {f.id for f in result.facts} == {F_BUDGET, F_CHARGED}
        assert result.related_chunks == []
        assert result.withheld_facts == 1 and result.withheld_related_chunks == 1
        assert ROSSI not in {e.id for e in result.entities}
        assert probe.probed == [{FINANCE}]  # the HR document was never even probed in this scope

    @pytest.mark.asyncio
    async def test_entitled_scope_probes_reached_documents_and_still_withholds_hr_from_alice(self, alice):
        expander, _, probe = _expander()
        result = await expander.expand(NS, [FIN_KB], scope=SCOPE_ENTITLED_DOCUMENTS)
        assert probe.probed == [{FINANCE}, {HR}]  # one probe per newly reached document
        assert result.withheld_facts == 1 and result.withheld_related_chunks == 1
        assert result.related_chunks == []

    @pytest.mark.asyncio
    async def test_entitled_scope_crosses_documents_for_a_caller_who_may_read_both(self):
        ACL["dana@example.com"] = {FINANCE, HR}
        token = set_request_user("dana@example.com")
        try:
            expander, _, _ = _expander()
            result = await expander.expand(NS, [FIN_KB], scope=SCOPE_ENTITLED_DOCUMENTS)
        finally:
            reset_request_user(token)
            del ACL["dana@example.com"]
        assert {f.id for f in result.facts} == {F_BUDGET, F_CHARGED, F_MANAGED}
        managed = next(f for f in result.facts if f.id == F_MANAGED)
        assert managed.chunk_id == HR_KB and managed.source_document_id == HR
        assert [(r.chunk_id, r.source_document_id, r.entity_ids) for r in result.related_chunks] == [
            (HR_KB, HR, (ATLAS,))
        ]
        assert ROSSI in {e.id for e in result.entities}
        assert result.withheld_facts == 0 and result.withheld_related_chunks == 0

    @pytest.mark.asyncio
    async def test_withheld_items_leave_no_trace_in_the_serialized_response(self, alice):
        expander, _, _ = _expander()
        payload = str((await expander.expand(NS, [FIN_KB], scope=SCOPE_ENTITLED_DOCUMENTS)).to_dict())
        for secret in (HR, HR_KB, HR_CHUNK, "Rossi", F_MANAGED):
            assert secret not in payload

    @pytest.mark.asyncio
    async def test_max_facts_truncation_is_counted_as_withheld(self):
        ACL["dana@example.com"] = {FINANCE, HR}
        token = set_request_user("dana@example.com")
        try:
            expander, store, _ = _expander()
            result = await expander.expand(NS, [FIN_KB], scope=SCOPE_ENTITLED_DOCUMENTS, max_facts=2)
        finally:
            reset_request_user(token)
            del ACL["dana@example.com"]
        assert len(result.facts) == 2 and result.withheld_facts == 1
        assert "LIMIT 2" in store.queries[1][0]  # hop 1 bounded by maxFacts

    @pytest.mark.asyncio
    async def test_namespace_without_a_knowledge_base_is_not_gated(self):
        """Gate does not apply (no per-user KB): everything the graph holds passes, no probes."""
        expander, _, probe = _expander(applies=False)
        result = await expander.expand(NS, [FIN_KB], scope=SCOPE_SEED_DOCUMENTS)
        assert {f.id for f in result.facts} == {F_BUDGET, F_CHARGED}  # seed scope still cuts hop 2
        assert probe.probed == []
        result = await expander.expand(NS, [FIN_KB], scope=SCOPE_ENTITLED_DOCUMENTS)
        assert {f.id for f in result.facts} == {F_BUDGET, F_CHARGED, F_MANAGED}

    @pytest.mark.asyncio
    async def test_invalid_scope_is_rejected_before_any_query(self, alice):
        expander, store, _ = _expander()
        with pytest.raises(ValueError, match="scope"):
            await expander.expand(NS, [FIN_KB], scope="EVERYTHING")
        assert store.queries == []


class TestCypher:
    """The templates reach the store fully formatted with this namespace's tenant labels."""

    @pytest.mark.asyncio
    async def test_every_placeholder_is_filled_and_ids_travel_as_parameters(self, alice):
        expander, store, _ = _expander()
        await expander.expand(NS, [FIN_KB], scope=SCOPE_SEED_DOCUMENTS)
        assert len(store.queries) == 3
        for cypher, _params in store.queries:
            assert "{" not in cypher and "}" not in cypher, cypher
            assert TENANT in cypher  # tenant-qualified labels, not bare toolkit labels
            assert FIN_KB not in cypher  # ids are parameters, never interpolated
        assert store.queries[0][1] == {"chunk_ids": [FIN_CHUNK]}
        assert store.queries[2][1] == {"entity_ids": [ATLAS, BASEL], "seed_chunk_ids": [FIN_CHUNK]}

    def test_templates_name_the_toolkit_edges_in_the_verified_directions(self):
        # Mirrors document_entitlement.py: statement -MENTIONED_IN-> chunk, fact -SUPPORTS-> statement,
        # entity -SUBJECT|OBJECT-> fact, chunk -EXTRACTED_FROM-> source.
        assert "<-[:`__MENTIONED_IN__`]-" in SEED_FACTS and "<-[:`__SUPPORTS__`]-" in SEED_FACTS
        assert "-[:`__SUPPORTS__`]->" in RELATED_FACTS and "-[:`__MENTIONED_IN__`]->" in RELATED_FACTS
        assert "-[:`__EXTRACTED_FROM__`]->" in SEED_PROVENANCE and "-[:`__EXTRACTED_FROM__`]->" in RELATED_FACTS
        assert "NOT id(c) IN $seed_chunk_ids" in RELATED_FACTS
