# COA outer agent

Optional AgentCore Runtime implementing semantic planning, source-native execution, bounded evidence assembly, and one final generation.

## Request

The public request accepts only `query` and `namespace`. AgentCore validates the JWT; the request body cannot supply user identity, groups, filters, endpoints, or target IDs.

## Flow

1. Call COA MCP `semantic_execution_plan` with the caller JWT.
2. Strictly validate plan version, status, ontology version, directives, and read-only invariant.
3. Resolve directive target IDs against `SOURCE_TARGETS_JSON` supplied by the deployment.
4. Execute all required source directives or stop.
5. Pivot into COA's graph (ADR 0012): hand the `_chunk_id` of every `MANAGED_KB` hit to COA MCP `expand_chunks` with the same JWT; the facts COA returns (only from documents the user may read, verified again by COA) join the evidence as `COA_GRAPH` items. If COA cannot expand, the trace records `coa.expand_chunks: unavailable` and the answer is generated from the source evidence alone.
6. Bound and label evidence.
7. Call Portkey once. The response is accepted only when every claim cites evidence IDs present in the authorized context.

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
  "knowledge-hub": {
    "kind": "MANAGED_KB",
    "knowledgeBaseIds": ["KBTEST0001"],
    "region": "eu-central-1",
    "dataSourceIds": ["DSTEST0001"]
  },
  "domain-query": { "kind": "GOVERNED_QUERY" },
  "domain-graph": { "kind": "EXTERNAL_GRAPH" }
}
```

A `MANAGED_KB` target may name the `region` its Knowledge Bases live in (default: the runtime's Region; the agent builds one Bedrock client per Region and the CDK stack grants `bedrock:Retrieve` on that Region's KB ARNs) and `dataSourceIds` (0-10) that limit retrieval to those data sources through a `_data_source_id` filter, the same scope a COA-registered `BEDROCK_MANAGED_KB` source carries. Both fields are refused on any other kind.

The namespace profile names only these logical IDs. Unknown fields, endpoints, credentials, malformed IDs, kind mismatches, and unavailable adapter kinds fail closed.

`COA_GRAPH_EXPANSION_SCOPE` controls step 5: `SEED_DOCUMENTS` (default) keeps the graph context inside the documents the Knowledge Base returned; `ENTITLED_DOCUMENTS` lets COA follow shared entities into every document the Knowledge Base shows the caller (one entitlement probe per document reached); `OFF` disables the step. `COA_GRAPH_EXPANSION_MAX_FACTS` (default 100, max 500) bounds the facts asked for.

## Validation

Run:

```bash
uv run pytest packages/outer-agent/tests -q
uv run ruff check packages/outer-agent
uv run mypy packages/outer-agent/src --ignore-missing-imports
```
