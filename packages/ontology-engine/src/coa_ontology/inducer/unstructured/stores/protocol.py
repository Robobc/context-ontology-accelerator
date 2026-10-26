# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Abstract interface for the lexical graph store.

This is a thin, typed Protocol that all lexical graph backends implement.
It covers the subset of operations the unstructured induction pipeline
uses to read class/entity/fact/topic data from a graphrag-toolkit
lexical knowledge graph.

Follows the same pattern as ``stores/base.py``:
  - runtime-checkable Protocol class
  - typed method signatures
  - dataclass record types for structured returns

Two golden rules:
  1. If a caller needs backend-specific behaviour, it talks to the
     concrete backend, not through this Protocol.
  2. Method names reflect the lexical graph domain model
     (__SYS_Class__, __Entity__, __Fact__, __Topic__).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# ── Record types ────────────────────────────────────────────────────────


@dataclass
class ClassRecord:
    """One classification bucket from a __SYS_Class__ node.

    Attributes:
        value: The class label (e.g. "Person", "Company").
        count: Number of entities in this classification.
    """

    value: str
    count: int


@dataclass
class RelationRecord:
    """One inter-class relationship from a __SYS_RELATION__ edge.

    Attributes:
        source_class: The source class label.
        predicate: The predicate label on the relationship.
        target_class: The target class label.
        count: Frequency of this predicate between the two classes.
    """

    source_class: str
    predicate: str
    target_class: str
    count: int


@dataclass
class EntityRecord:
    """One entity from a __Entity__ node.

    Attributes:
        entity_id: The unique entity identifier.
        value: The entity surface-form label.
        classification: The classification string (class membership).
    """

    entity_id: str
    value: str
    classification: str


@dataclass
class FactRecord:
    """One fact from a __Fact__ node.

    A fact is either SPO (subject-predicate-object, entity-to-entity)
    or SPC (subject-predicate-complement, entity-to-literal).

    Attributes:
        subject_value: The subject entity's surface-form label.
        subject_class: The subject entity's classification.
        predicate: The predicate label.
        object_value: The object entity's label (None for SPC facts).
        object_class: The object entity's classification (None for SPC facts).
        complement: The complement literal string (None for SPO facts).
    """

    subject_value: str
    subject_class: str
    predicate: str
    object_value: str | None
    object_class: str | None
    complement: str | None


@dataclass
class TopicRecord:
    """One topic from a __Topic__ node.

    Attributes:
        topic_id: The unique topic identifier.
        value: The topic label/description.
        statement_count: Number of connected __Statement__ nodes.
    """

    topic_id: str
    value: str
    statement_count: int


@dataclass
class ClassSchemaResult:
    """Combined schema result from get_graph_schema().

    Attributes:
        classes: List of class records with labels and entity counts.
        relations: List of inter-class relationship records.
    """

    classes: list[ClassRecord]
    relations: list[RelationRecord]


# ── Protocol ────────────────────────────────────────────────────────────


@runtime_checkable
class LexicalGraphStore(Protocol):
    """Backend-agnostic interface for reading a lexical knowledge graph.

    Implementations exist for Neptune Analytics (using the Property Graph
    Schema API for aggregates) and Neptune DB (using openCypher queries).

    The pipeline interacts with the lexical graph exclusively through this
    Protocol — it never imports or references a concrete backend directly.
    """

    def get_class_nodes(self) -> list[ClassRecord]:
        """Retrieve all __SYS_Class__ nodes with their labels and counts.

        Returns:
            List of ClassRecord instances, one per distinct classification.
        """
        ...

    def get_class_relations(self) -> list[RelationRecord]:
        """Retrieve all __SYS_RELATION__ edges between __SYS_Class__ nodes.

        Returns:
            List of RelationRecord instances representing inter-class
            relationships with their predicate labels and frequencies.
        """
        ...

    def get_entities_by_class(
        self,
        classification: str,
        limit: int = 100,
    ) -> list[EntityRecord]:
        """Retrieve __Entity__ nodes for a given classification.

        Args:
            classification: The classification string to filter by
                (case-sensitive match on the entity's ``class`` property).
            limit: Maximum number of entities to return.

        Returns:
            List of EntityRecord instances. Returns an empty list if no
            entities match the classification (does not raise).
        """
        ...

    def get_facts_for_entities(
        self,
        entity_ids: list[str],
    ) -> list[FactRecord]:
        """Retrieve __Fact__ nodes for a set of entity IDs.

        Returns both SPO facts (object entity populated) and SPC facts
        (complement populated). Facts with neither are excluded.

        When the entity ID set exceeds 500 IDs, implementations MUST
        batch the query into chunks of at most 500 IDs per request.

        Args:
            entity_ids: List of entity identifiers to retrieve facts for.

        Returns:
            List of FactRecord instances.
        """
        ...

    def get_topics(self) -> list[TopicRecord]:
        """Retrieve all __Topic__ nodes with their connected statement counts.

        Topics with zero connected statements are included with
        statement_count=0 (not omitted).

        Returns:
            List of TopicRecord instances.
        """
        ...

    def has_access_controlled_sources(self) -> bool:
        """True when the lexical graph was built from an access-controlled document store.

        kg-build gives documents loaded from a Bedrock Knowledge Base with
        document-level ACL a ``__Source__`` node id prefixed ``aws:doc:``. Such a
        graph holds every document's facts under one tenant while the Knowledge
        Base authorizes per user; promoting its entities into a namespace-wide
        ontology would leak restricted documents into every caller's view, so
        induction over it is refused (fork, ADR 0011).
        """
        ...

    def get_graph_schema(self) -> ClassSchemaResult:
        """Retrieve the class distribution and inter-class relationships.

        This is the primary entry point for T-Box schema information.
        Neptune Analytics implementations SHOULD use the Property Graph
        Schema API (pg_schema) for efficiency. Neptune DB implementations
        fall back to openCypher queries against __SYS_Class__ and
        __SYS_RELATION__ nodes.

        Returns:
            ClassSchemaResult containing class records and relation records.

        Raises:
            RuntimeError: If the graph store is unreachable or returns an
                error (within 120s timeout).
            ValueError: If the schema contains zero class entries.
        """
        ...

    def health_check(self) -> dict:
        """Cheap probe returning backend health status.

        Returns:
            Dict with at minimum:
              - "status": "healthy" or "unhealthy"
              - "backend": implementation type identifier
                (e.g. "neptune_analytics", "neptune_db")
        """
        ...
