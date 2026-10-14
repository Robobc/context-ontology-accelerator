# ADR 0008: Domain agent owns source-native execution

## Status

Accepted.

## Decision

A JWT-authenticated outer/domain agent validates `SemanticExecutionPlan`, executes every required directive through an administrator-configured native adapter, assembles bounded evidence, and invokes the final generation provider once.

Managed KB uses direct `AgenticRetrieveStream` with `generateResponse=false`; the runtime role authorizes the API call and token-derived `userContext` preserves the human document filter. Governed query and external graph adapters fail closed until their owners provide OBO/query contracts.

An IAM-only AgentCore Gateway was rejected for this human-delegated path because it carries no human OBO/transitive identity. The public COA MCP endpoint remains JWT-authenticated.

## Consequences

Source systems retain execution, authorization, freshness, and provenance. Failure of any required directive prevents generation from a partial evidence set. The stock `query` operation remains optional compatibility mode.
