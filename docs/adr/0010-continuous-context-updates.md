# ADR 0010: Continuous context updates are candidate-driven

## Status

Proposed.

## Decision

Build continuous updates as detect → fingerprint → impact → candidate delta → validate → approve → publish/reconcile. Reuse COA 0.3.5 recurring/event-driven rescans, schema diffs, induction, R2RML, SHACL, proposal review, and versioned artifacts.

Automatic publication is limited to non-semantic additive changes that pass contract tests. Semantic meaning, identity rules, constraints, removals, and other breaking changes require owner approval and rollback data.

## Consequences

The system does not claim that ontologies update themselves. Impact records must span TBox, R2RML, SHACL, ABox projections, entity indexes, retrieval indexes, competency questions, and source adapters. Full implementation remains a separate workstream.
