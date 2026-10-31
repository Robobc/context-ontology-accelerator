# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fork (ADR 0012): expand the lexical graph from Knowledge Base chunks a caller retrieved.

The agent-driven document path: the caller's agent runs ``Retrieve`` against the
Bedrock Knowledge Base itself, as the user, and hands COA the ``_chunk_id`` values
it got back. COA maps them to ``__Chunk__`` nodes (``coa_common.kb_ids``) and walks
two hops of graphrag-toolkit's lexical graph::

    seed __Chunk__ <-MENTIONED_IN- __Statement__ <-SUPPORTS- __Fact__ <-SUBJECT|OBJECT- __Entity__
    __Entity__ -SUBJECT|OBJECT-> __Fact__ -SUPPORTS-> __Statement__ -MENTIONED_IN-> other __Chunk__

Hop 1 yields the facts and entities of the seed chunks; hop 2 yields the facts
and chunks of other documents that mention those entities. The caller is never
trusted about what it retrieved: every seed chunk's document is checked against
the Knowledge Base under the caller's identity through the shared
:class:`DocumentEntitlementGate` (a chunk the caller could not retrieve is not
expanded), and hop 2 is cut to the seed documents (``SEED_DOCUMENTS``) or to
the documents the Knowledge Base shows the caller (``ENTITLED_DOCUMENTS``, one
probe per newly reached document, cached by the gate). Withheld items are
counted, never named: a count does not confirm that a document exists.

Chunk text is not returned. The caller fetches it from the Knowledge Base, which
is the system of record for the text and for who may read it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from coa_common.constants import to_graphrag_tenant_id
from coa_common.kb_ids import bedrock_chunk_id_of, stored_chunk_id

logger = structlog.get_logger(__name__)

SCOPE_SEED_DOCUMENTS = "SEED_DOCUMENTS"
SCOPE_ENTITLED_DOCUMENTS = "ENTITLED_DOCUMENTS"
SCOPES = frozenset({SCOPE_SEED_DOCUMENTS, SCOPE_ENTITLED_DOCUMENTS})

# Seed chunk -> its document. Only chunks that exist and have a source are rows.
SEED_PROVENANCE: str = (
    "MATCH (c:{chunk_label})-[:`__EXTRACTED_FROM__`]->(s:{source_label})\n"
    "WHERE id(c) IN $chunk_ids\n"
    "RETURN id(c) AS chunkId, s.source_uri AS uri"
)
# Hop 1: the facts extracted from the seed chunks, with subject and object
# entities. Edge directions as in document_entitlement.py (verified against
# graphrag-lexical-graph 3.18.7): statement -MENTIONED_IN-> chunk,
# fact -SUPPORTS-> statement, entity -SUBJECT|OBJECT-> fact.
SEED_FACTS: str = (
    "MATCH (c:{chunk_label})<-[:`__MENTIONED_IN__`]-(st:{statement_label})<-[:`__SUPPORTS__`]-(f:{fact_label})\n"
    "WHERE id(c) IN $chunk_ids\n"
    "MATCH (subj:{entity_label})-[:`__SUBJECT__`]->(f)\n"
    "OPTIONAL MATCH (obj:{entity_label})-[:`__OBJECT__`]->(f)\n"
    "RETURN DISTINCT id(c) AS chunkId, id(f) AS factId, f.value AS fact,\n"
    "       id(subj) AS subjectId, subj.value AS subjectLabel, subj.class AS subjectClass,\n"
    "       id(obj) AS objectId, obj.value AS objectLabel, obj.class AS objectClass\n"
    "LIMIT {limit}"
)
# Hop 2: from the hop-1 entities to facts mentioned in OTHER chunks, with the
# chunk's document so the scope can be applied before anything is returned.
RELATED_FACTS: str = (
    "MATCH (e:{entity_label})-[:`__SUBJECT__`|`__OBJECT__`]->(f:{fact_label})-[:`__SUPPORTS__`]->"
    "(st:{statement_label})-[:`__MENTIONED_IN__`]->(c:{chunk_label})-[:`__EXTRACTED_FROM__`]->(s:{source_label})\n"
    "WHERE id(e) IN $entity_ids AND NOT id(c) IN $seed_chunk_ids\n"
    "MATCH (subj:{entity_label})-[:`__SUBJECT__`]->(f)\n"
    "OPTIONAL MATCH (obj:{entity_label})-[:`__OBJECT__`]->(f)\n"
    "RETURN DISTINCT id(e) AS viaEntityId, id(c) AS chunkId, s.source_uri AS uri, id(f) AS factId, f.value AS fact,\n"
    "       id(subj) AS subjectId, subj.value AS subjectLabel, subj.class AS subjectClass,\n"
    "       id(obj) AS objectId, obj.value AS objectLabel, obj.class AS objectClass\n"
    "LIMIT {limit}"
)
# Hop 2 reads up to this many rows per returned fact slot; related chunks are
# many-to-many with facts, so the row budget is wider than the fact budget.
_RELATED_ROW_FACTOR = 4


@dataclass(frozen=True)
class LexicalEntity:
    """An ``__Entity__`` node: id, surface form, extractor class."""

    id: str
    label: str
    class_: str | None


@dataclass(frozen=True)
class LexicalFact:
    """A ``__Fact__`` node with the Knowledge Base chunk and document it came from."""

    id: str
    text: str
    subject_id: str
    object_id: str | None
    chunk_id: str  # Knowledge Base _chunk_id, not the graph node id
    source_document_id: str


@dataclass(frozen=True)
class RelatedChunk:
    """A chunk of another document that mentions one or more returned entities."""

    chunk_id: str  # Knowledge Base _chunk_id
    source_document_id: str
    entity_ids: tuple[str, ...]


@dataclass
class ExpansionResult:
    """What :meth:`ChunkExpander.expand` returns; counts, not names, for what was withheld."""

    entities: list[LexicalEntity] = field(default_factory=list)
    facts: list[LexicalFact] = field(default_factory=list)
    related_chunks: list[RelatedChunk] = field(default_factory=list)
    withheld_seed_chunks: int = 0
    withheld_facts: int = 0
    withheld_related_chunks: int = 0

    def to_dict(self) -> dict[str, Any]:
        """The Smithy ``GraphExpandChunks`` output members (without ``trace``)."""
        return {
            "entities": [
                {"id": e.id, "label": e.label, **({"class": e.class_} if e.class_ else {})} for e in self.entities
            ],
            "facts": [
                {
                    "id": f.id,
                    "text": f.text,
                    "subjectId": f.subject_id,
                    **({"objectId": f.object_id} if f.object_id else {}),
                    "chunkId": f.chunk_id,
                    "sourceDocumentId": f.source_document_id,
                }
                for f in self.facts
            ],
            "relatedChunks": [
                {"chunkId": r.chunk_id, "sourceDocumentId": r.source_document_id, "entityIds": list(r.entity_ids)}
                for r in self.related_chunks
            ],
            "withheld": {
                "seedChunks": self.withheld_seed_chunks,
                "facts": self.withheld_facts,
                "relatedChunks": self.withheld_related_chunks,
            },
        }


class ChunkExpander:
    """Walk the lexical graph outward from Knowledge Base chunks, within the caller's documents."""

    def __init__(self, entitlement_gate: Any) -> None:
        """``entitlement_gate`` is the service's shared :class:`DocumentEntitlementGate`."""
        self._gate = entitlement_gate

    async def expand(
        self,
        namespace: str,
        bedrock_chunk_ids: list[str],
        *,
        scope: str = SCOPE_SEED_DOCUMENTS,
        max_facts: int = 100,
        user_id: str | None = None,
    ) -> ExpansionResult:
        """Expand from ``bedrock_chunk_ids`` (Knowledge Base ``_chunk_id`` values) for the request's caller."""
        if scope not in SCOPES:
            raise ValueError(f"Invalid scope. Must be one of: {', '.join(sorted(SCOPES))}.")
        result = ExpansionResult()
        tenant = to_graphrag_tenant_id(namespace)
        requested = list(dict.fromkeys(bedrock_chunk_ids))  # order kept, duplicates dropped
        stored = [stored_chunk_id(tenant, cid) for cid in requested]

        # Seed provenance, then the caller's entitlement to each seed document.
        rows = await self._gate.run_query(SEED_PROVENANCE, namespace, {"chunk_ids": stored})
        uri_of_seed = {str(r.get("chunkId")): str(r.get("uri") or "") for r in rows if r.get("uri")}
        allowed = await self._allowed(namespace, set(uri_of_seed.values()), user_id)
        seeds = [cid for cid in stored if uri_of_seed.get(cid) in allowed]
        result.withheld_seed_chunks = len(requested) - len(seeds)
        if not seeds:
            return result

        # Hop 1: the seeds' own facts.
        rows = await self._gate.run_query(
            SEED_FACTS.format(limit=max_facts, **_LABEL_PLACEHOLDERS),
            namespace,
            {"chunk_ids": seeds},
            fact_label="__Fact__",
        )
        entities: dict[str, LexicalEntity] = {}
        facts: dict[str, LexicalFact] = {}
        for r in rows:
            self._add_fact(r, uri_of_seed, entities, facts)
        if not facts:
            return result

        # Hop 2: other chunks mentioning the hop-1 entities, cut to scope.
        rows = await self._gate.run_query(
            RELATED_FACTS.format(limit=max_facts * _RELATED_ROW_FACTOR, **_LABEL_PLACEHOLDERS),
            namespace,
            {"entity_ids": sorted(entities), "seed_chunk_ids": seeds},
            fact_label="__Fact__",
        )
        reached_uris = {str(r.get("uri") or "") for r in rows if r.get("uri")}
        if scope == SCOPE_ENTITLED_DOCUMENTS:
            allowed |= await self._allowed(namespace, reached_uris - allowed, user_id)
        related: dict[str, tuple[str, set[str]]] = {}
        withheld_chunks: set[str] = set()
        withheld_facts: set[str] = set()
        for r in rows:
            chunk, uri, fact_id = str(r.get("chunkId")), str(r.get("uri") or ""), str(r.get("factId"))
            if uri not in allowed:
                withheld_chunks.add(chunk)
                if fact_id not in facts:
                    withheld_facts.add(fact_id)
                continue
            via = related.setdefault(chunk, (uri, set()))
            via[1].add(str(r.get("viaEntityId")))
            if len(facts) < max_facts:
                self._add_fact(r, {chunk: uri}, entities, facts)
            elif fact_id not in facts:
                withheld_facts.add(fact_id)  # beyond maxFacts; the caller can raise it
        # A fact or chunk seen first through a denied document and later through
        # an allowed one is returned, so it is not withheld.
        result.withheld_facts = len(withheld_facts - set(facts))
        result.withheld_related_chunks = len(withheld_chunks - set(related))

        result.entities = sorted(entities.values(), key=lambda e: e.id)
        result.facts = list(facts.values())
        for chunk, (uri, via) in related.items():
            kb_id = bedrock_chunk_id_of(chunk)
            if kb_id:  # a toolkit-minted chunk has no Knowledge Base id to hand back
                result.related_chunks.append(RelatedChunk(kb_id, uri, tuple(sorted(via))))
        if result.withheld_seed_chunks or result.withheld_facts or result.withheld_related_chunks:
            logger.info(
                "chunk_expand_withheld",
                namespace=namespace,
                seed_chunks=result.withheld_seed_chunks,
                facts=result.withheld_facts,
                related_chunks=result.withheld_related_chunks,
            )
        return result

    async def _allowed(self, namespace: str, uris: set[str], user_id: str | None) -> set[str]:
        """Documents among ``uris`` the caller may read; every document when the gate does not apply."""
        if not uris:
            return set()
        if not self._gate.applies_to(namespace):
            return set(uris)
        return set(await self._gate.entitled_uris(namespace, uris, user_id))

    @staticmethod
    def _add_fact(
        row: dict, uri_of_chunk: dict[str, str], entities: dict[str, LexicalEntity], facts: dict[str, LexicalFact]
    ) -> None:
        fact_id = str(row.get("factId") or "")
        chunk = str(row.get("chunkId") or "")
        kb_chunk_id = bedrock_chunk_id_of(chunk)
        subject_id = str(row.get("subjectId") or "")
        if not fact_id or not subject_id or not kb_chunk_id or fact_id in facts:
            return
        object_id = row.get("objectId")
        object_id = str(object_id) if object_id else None
        entities.setdefault(
            subject_id, LexicalEntity(subject_id, str(row.get("subjectLabel") or ""), row.get("subjectClass") or None)
        )
        if object_id:
            entities.setdefault(
                object_id, LexicalEntity(object_id, str(row.get("objectLabel") or ""), row.get("objectClass") or None)
            )
        facts[fact_id] = LexicalFact(
            id=fact_id,
            text=str(row.get("fact") or ""),
            subject_id=subject_id,
            object_id=object_id,
            chunk_id=kb_chunk_id,
            source_document_id=uri_of_chunk.get(chunk, ""),
        )


# ``str.format`` is applied twice to the two-hop templates: once here for the
# integer ``limit`` (never caller text), once in the gate for the labels. These
# placeholders survive the first pass so the gate can fill them.
_LABEL_PLACEHOLDERS = {
    name: "{" + name + "}" for name in ("chunk_label", "source_label", "entity_label", "statement_label", "fact_label")
}
