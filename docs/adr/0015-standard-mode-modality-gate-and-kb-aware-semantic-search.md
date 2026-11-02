# ADR 0015: A Decider-backed modality gate in front of the Standard cascade, and Knowledge-Base-aware semantic search in the reasoning loop

## Status

Proposed, implemented on `feature/standard-modality-gate` (2026-11-02) for
release `v0.3.7-eba.3`. Follows ADR 0014 (the Decider) and ADR 0011/0012 (a
Bedrock Knowledge Base as a document source). Opt-in: the Standard-mode gate is
off unless `SERVE_TIER2_SKIP_MODE` names it; the loop change is active only on
namespaces that bind a Knowledge Base, where the previous path returned nothing.

## Context

Two observations from the `v0.3.7-eba.2` verification on the mixed PPM
namespace (data-vault tables plus a Knowledge Base of governance memos with
per-document ACLs), question *"What did the stage-gate committee decide about
project PI0013025 and what condition did it attach?"*:

1. **Standard mode answered a document question from a table.** The cascade
   reached Tier 2, the ontology carried columns named `Decision` and
   `Condition`, the generated SQL returned four rows, and the cascade stopped.
   The memo that records the decision was never read. The existing Tier-2 skip
   evaluator (`AossProvenanceEvaluator`) could not prune Tier 2: it answers
   "does the query retrieve any *mapped* class?", and those columns are mapped.
   Routing *away* from Tier 2 needs a judgement about the question, not about
   the ontology.
2. **Deep reasoning never reached the memo.** The agentic loop's semantic
   search tools (`vector_search` and the graphrag strategy tools) read COA's own
   OpenSearch chunk index, which a Knowledge-Base namespace does not populate
   (the KB owns the chunks, ADR 0011). The final semantic search returned 0
   chunks; documents were reachable only through graph traversal from entities
   the planner happened to touch. The same question through the MCP
   `rag_retrieval` tool returned the memo in the top 3 (score 0.889), because
   that path uses the namespace-aware document retriever and Bedrock Retrieve as
   the signed-in user.

ADR 0014 built a cheap, measured classifier (97.4 % modality accuracy on the
65-item labelled set with Nova Micro) and wired it only inside deep reasoning.
Both observations are the same gap seen from two sides: the Decider's judgement
is not available to the Standard cascade, and the loop's document tool is not
aware of where the documents are.

## Decision

### D1. `DeciderTier2SkipEvaluator` — the Decider as a Tier-2 skip evaluator

A new implementation of the existing `Tier2SkipEvaluator` protocol
(`tier2/skip/decider_skip.py`). It asks the Decider one modality question with
the whole query as the single sub-question and maps the answer onto the
protocol's fail-open contract:

| Decider answer | Decision | Effect |
|---|---|---|
| `document`, confidence ≥ `minConfidence` | `SKIP` | Tier 2 pruned; Tier 3 runs (on a KB namespace: Bedrock Retrieve as the user, then synthesis) |
| `tabular` or `mixed` | `RUN` | Tier 2 runs as today |
| abstention, low confidence, backend `off`, any error | `ABSTAIN` → optional fallback | The AOSS evaluator decides, or Tier 2 runs |

No orchestrator change: the hook, the `tierOverride` / explicit-strategy
bypasses and the Tier-1 metric path are untouched. A certified metric still
wins before the gate is consulted.

Wiring is `SERVE_TIER2_SKIP_MODE`: `aoss` (default, upstream behaviour),
`decider`, `decider+aoss` (Decider first, AOSS on abstention), `off`. CDK
context `tier2_skip_mode` / `SCL_TIER2_SKIP_MODE`. The live switch is the
Decider's own SSM parameter: `backend=off` makes the gate defer to the fallback
within the TTL, so an operator can turn it off without a redeploy.

The trace records the decision as a `routing.select` step with
`{"evaluator": "decider", "modality", "confidence", "model", "decision"}`.

### D2. Knowledge-Base-aware `vector_search` in the reasoning loop

`VectorSearchTool` accepts the namespace-aware `DocumentRetriever` the serve
layer already uses for KBSearch. When `requires_query_embedding(namespace)` is
false (a managed Knowledge Base backs the namespace) the tool retrieves through
it with the caller's identity (`profile.email`, else the request-scoped user),
so Bedrock applies the document ACLs; no local embedding, no entitlement gate
(the KB already filtered). Other namespaces keep the OpenSearch path unchanged.

The agentic retriever prefers this tool over the graphrag strategy tool for the
**final semantic search** when `handles_namespace(namespace)` is true. The
planner may still pick a strategy tool mid-loop on such a namespace and get
nothing; the final search now covers that case with the KB.

### D3. Not changed

- Code defaults: `SERVE_TIER2_SKIP_MODE=aoss`, planner profile `legacy`,
  Decider `off`. A deployment without the knobs behaves as before.
- The Decider's "named document source" rule (ADR 0014, deferred) and
  `check_answerable` stay deferred.
- Entity-to-row identity (a document's `PI0013025` and the table's `PI_ID`) is
  still a shared-identifier convention, not a link (see the fork README).

## Consequences

- A document question in Standard mode on a mixed namespace reaches the
  documents when the Decider is confident. Cost: one Nova Micro call
  (~0.5 s) per Standard query on deployments that opt in.
- The Decider's measured errors are all `document → tabular` (ADR 0014); in
  this gate such an error yields `RUN`, i.e. today's behaviour. The inverse
  error (`tabular → document`) was not observed on the labelled set; if it
  occurs, Tier 2 is skipped for a tabular question and Tier 3 answers from
  documents or returns nothing. The `decider+aoss` mode does not protect against
  it (AOSS is consulted only on abstention); the mitigation is the confidence
  threshold and the live `off` switch.
- Deep reasoning on a KB namespace now ends with a KB retrieval as the user,
  so the synthesizer sees the entitled documents even when the graph had no
  path to them.

## Verification

Unit (`packages/context-manager/tests/unit`): `test_tier2_skip_decider.py`
(7: skip on confident document with trace, run on tabular/mixed, fallback on low
confidence, abstain without fallback, backend off defers without a model call,
error is fail-open, whole question is the single sub-question);
`test_tier3_agentic_vector_tool_kb.py` (6: KB path as the user without
embedding, OpenSearch path unchanged, KB error degrades, no query text degrades,
`handles_namespace`, builder plumbing); two final-search preference tests in
`test_tier3_agentic_retriever.py`. Infra: `serve-stack.test.ts` asserts no
`SERVE_TIER2_SKIP_MODE` by default and the context override.

Deployed verification on dev is recorded in the release notes for
`v0.3.7-eba.3` (Standard-mode and deep-reasoning probes on the mixed namespace,
same reader, same question as the observations above).
