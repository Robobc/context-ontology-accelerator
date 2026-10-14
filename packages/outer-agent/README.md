# COA outer agent

Optional AgentCore Runtime implementing semantic planning, source-native execution, bounded evidence assembly, and one final generation.

## Request

The public request accepts only `query` and `namespace`. AgentCore validates the JWT; the request body cannot supply user identity, groups, filters, endpoints, or target IDs.

## Flow

1. Call COA MCP `semantic_execution_plan` with the caller JWT.
2. Strictly validate plan version, status, ontology version, directives, and read-only invariant.
3. Resolve directive target IDs against `SOURCE_TARGETS_JSON` supplied by the deployment.
4. Execute all required source directives or stop.
5. Bound and label evidence.
6. Call Portkey once. The response is accepted only when every claim cites evidence IDs present in the authorized context.

## Source adapters

- `MANAGED_KB`: implemented with direct `AgenticRetrieveStream`, generation disabled, and token-derived `userContext`.
- `GOVERNED_QUERY`: target/config contract only; fails closed until an OBO or governed query-broker contract exists.
- `EXTERNAL_GRAPH`: target/config contract only; fails closed until a governed read-only query contract exists.
- `COA_GRAPH`: planning contract only in this runtime.

Deployments register source-owned implementations by target kind through the public `SourceAdapter` protocol. The registry validates each target's configured kind before dispatch and rejects attempts to replace the reserved built-in `MANAGED_KB` adapter.

## Configuration

`SOURCE_TARGETS_JSON` is an administrator-owned map. Example:

```json
{
  "domain-kb": {
    "kind": "MANAGED_KB",
    "knowledgeBaseIds": ["ABCDEFGHIJ"],
    "maxAgentIterations": 5
  },
  "domain-query": { "kind": "GOVERNED_QUERY" },
  "domain-graph": { "kind": "EXTERNAL_GRAPH" }
}
```

The namespace profile names only these logical IDs. Unknown fields, endpoints, credentials, malformed IDs, kind mismatches, and unavailable adapter kinds fail closed.

## Validation

Run:

```bash
uv run pytest packages/outer-agent/tests -q
uv run ruff check packages/outer-agent
uv run mypy packages/outer-agent/src --ignore-missing-imports
```
