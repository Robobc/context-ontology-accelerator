# ADR 0006: Versioned semantic execution plan

## Status

Accepted.

## Decision

Expose `SemanticExecutionPlan` version `1.0` over MCP and REST. The plan preserves the original question, resolved concepts/entities, ambiguity, logical source directives, expected evidence, namespace profile version, and approved ontology version. It contains no source data, credentials, endpoints, or authorization grants.

Only `RESOLVED` plans emit directives. A legacy namespace without an approved ontology-version binding returns `AMBIGUOUS` and no directives.

## Consequences

The existing end-to-end `query` API remains compatible. Domain agents can depend on a stable planning contract while source adapters and graph implementations evolve independently. Contract changes require a new plan version and compatibility tests.
