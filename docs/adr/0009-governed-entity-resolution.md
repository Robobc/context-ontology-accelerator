# ADR 0009: Enterprise entity resolution is a governed boundary

## Status

Proposed; contract accepted, implementation blocked on domain identity sources.

## Decision

Keep canonical entities in the semantic plan contract, but do not infer production identity links from generated query labels. A production linker must support declared identifiers and aliases, confidence, provenance, freshness, authorization-aware candidate retrieval, and reviewed merge/split lifecycle.

## Consequences

The current planner returns no fabricated canonical entities. Domain slices that require identity resolution must provide canonical-key rules and representative ambiguity cases before they can pass acceptance. Bounded label search may be used only as a labelled fixture experiment, never as the production linker.
