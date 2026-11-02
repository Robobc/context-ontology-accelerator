# ADR 0012: Let an agent that retrieved from the Knowledge Base itself pivot into COA's graph

## Status

Proposed. Implemented on `feature/bedrock-kb-chunk-source` in three layers,
each committed on its own: serve (`GraphExpandChunks` on the data-layer
surface, `tier3/chunk_expander.py`), the REST edge and the `expand_chunks` MCP
tool, and the outer agent's chaining of `Retrieve` into `expand_chunks`.
Deployed to the dev deployment and run against the ACL fixture of ADR 0011 on
2026-10-31 (results under Verification): the pivot and the seed verification
held through MCP and REST for one real user; the cross-document cut and the
outer-agent leg are not yet exercised, for the reasons recorded there.

Extends ADR 0011 (a Knowledge Base is a source like any other; the graph is
gated per document) and ADR 0008 (the source-native outer agent). Changes
neither: it adds the one entry point ADR 0011's query path did not have, a
graph pivot whose vector leg is run by the caller rather than by COA.

## Context

ADR 0011 built the lexical graph from the Knowledge Base's own chunks, keyed
on the Knowledge Base's `_chunk_id`, so that a `Retrieve` hit *is* a graph
node. Its query path is COA-driven: the caller asks COA a question, COA runs
`Retrieve` as that user, pivots the hits into the graph, cuts the expansion to
the user's documents and answers. That is what the web console needs, where
nothing stands between the user and COA.

The customer's domain agents are built differently (ADR 0008, confirmed with
the customer on 2026-10-31): the agent owns retrieval. It calls COA
`semantic_execution_plan` for the grounded question, runs `Retrieve` against
the Knowledge Base *itself*, as the user, assembles the evidence and generates
once. In that shape COA was only ever asked for the plan; the graph it had
built from the same chunks was unreachable, because every COA graph read
started from a question COA itself had to retrieve for. The design that
started this work ("one ingestion, two retrieval modes", 2026-10-24) names the
pivot but not who drives it. The agent team expects to drive it.

What was missing is small: an operation that takes the chunk ids the agent
already holds and returns the graph context around them. What is not small is
the trust question it raises, because the caller now *claims* which chunks it
retrieved.

## Decision

### The operation

`GraphExpandChunks`, `POST /namespaces/{namespaceId}/graph/expand`, on the
`DataLayerService` beside `KBSearch` and `GraphTraverse`; serve action
`graphExpand`; MCP tool `expand_chunks`. Input: 1 to 50 Knowledge Base
`_chunk_id` values (the `KnowledgeBaseChunkId` grammar `[A-Za-z0-9._-]{1,256}`,
which is also `coa_common.kb_ids`' id grammar), an `ExpansionScope`, and
`maxFacts` (1 to 500, default 100). Output: `entities`, `facts` (each with the
chunk and document it came from), `relatedChunks` (other chunks the caller may
read that mention a returned entity, with the entity ids), `withheld` counts,
and the trace.

Two hops of graphrag-toolkit's lexical graph, in the edge directions ADR 0011
verified against `graphrag-lexical-graph` 3.18.7:

```
hop 1   seed __Chunk__ <-MENTIONED_IN- __Statement__ <-SUPPORTS- __Fact__ <-SUBJECT|OBJECT- __Entity__
hop 2   __Entity__ -SUBJECT|OBJECT-> __Fact__ -SUPPORTS-> __Statement__ -MENTIONED_IN-> other __Chunk__ -EXTRACTED_FROM-> __Source__
```

Hop 1 is the seeds' own facts and entities. Hop 2 is the facts and chunks of
*other* chunks that mention those entities, which is the cross-document
navigation a graph exists for, and exactly where ADR 0011's leak lives: an
entity is shared across documents, so from an allowed chunk the walk reaches
facts of documents the caller may not read.

Chunk text is never returned. The Knowledge Base stays the system of record
for the text and for who may read it; the caller fetches related chunks' text
there and may pass their ids back to keep navigating. Each call is therefore
one bounded step, and the entitlement work per step is one probe per newly
reached document.

### Trust: the caller is never believed about what it retrieved

An agent that could expand *any* chunk id would hold a per-document ACL
bypass, because chunk ids are content hashes a third party can know. So:

1. **Seeds are verified.** Every seed's document is resolved from the graph
   and checked against the Knowledge Base under the caller's identity through
   the shared `DocumentEntitlementGate` (same probe, same cache as ADR 0011's
   query path). A seed the caller could not have retrieved is not expanded.
2. **Unknown and denied are one case.** A chunk id the graph does not know and
   one in a denied document are both counted in `withheld.seedChunks`; nothing
   in the response distinguishes them, so a count never confirms that a
   document exists.
3. **Hop 2 is cut to a scope the caller chooses but cannot widen past the
   gate.** `SEED_DOCUMENTS` (default) keeps everything inside the documents of
   the verified seeds: no further probes, no new document can appear.
   `ENTITLED_DOCUMENTS` lets the walk cross into every document the Knowledge
   Base shows the caller, one probe per newly reached document. Either way,
   facts and chunks from documents outside the allowed set are counted in
   `withheld`, never named.
4. **Entity labels follow the fact.** An entity is returned only as the
   subject or object of a returned fact, so its surface form comes from a
   document the caller may read. This is ADR 0011's "at least one entitled
   document" rule applied at fact granularity.
5. **No caller, nothing.** Without a resolved request identity the gate
   returns no entitled documents and the expansion is empty; a namespace with
   no Knowledge Base source has no such chunk nodes and expands nothing.
6. **Same admission gate, same action.** The action sits behind serve's
   namespace admission gate like `kbSearch`; the REST route and the MCP tool
   are gated under `searchDocuments`, the action that already gates
   `kb/search` and `rag_retrieval`, because the response is document-derived
   content, not the ontology graph `traverseGraph` covers.
7. **Ids travel as query parameters, labels as tenant-formatted literals.**
   The three openCypher templates take the namespace's tenant-qualified
   labels from the gate and every id as a parameter; the only formatted
   integer is `maxFacts`, which is already range-checked. The chunk-id
   grammar is enforced at the REST edge, in the MCP tool and in serve.

### The outer agent

After the source directives run, the agent collects the `_chunk_id` of every
`MANAGED_KB` evidence item (the chunks the Knowledge Base just showed this
user), calls `expand_chunks` with the same bearer token, and appends the facts
as `COA_GRAPH` evidence, one per fact, cited like a chunk with its document as
source and numbered in one sequence with the Knowledge Base hits. Scope and
fact budget come from `COA_GRAPH_EXPANSION_SCOPE` (default `SEED_DOCUMENTS`;
`OFF` disables) and `COA_GRAPH_EXPANSION_MAX_FACTS`.

For the two sides to meet on the same chunks, the agent's `MANAGED_KB` target
has to read the same Knowledge Base scope COA scanned. A target in the
deployment's `outerAgent.sourceTargets` may therefore name its `region`
(default: the runtime's Region; one Bedrock client per Region, and the IAM
grant names that Region's KB ARNs) and `dataSourceIds` (0-10, a
`_data_source_id` retrieval filter), mirroring the `region` and
`dataSourceIds` of the registered `BEDROCK_MANAGED_KB` source, and `reranking`
(`MANAGED` or `NONE`: Bedrock rejects managed reranking on a Knowledge Base
with a custom embedding model, which every managed-search KB has, with
`ValidationException: Managed reranking is not supported for knowledge bases
with custom embedding models`). All three default to the previous behaviour
(runtime Region, whole Knowledge Base, managed reranking).

This is enrichment, not a plan directive. ADR 0008's rule that a failed
directive must never yield an answer from a partial set is about *sources* the
plan named; the graph is COA's derived view of the same documents. When COA
cannot expand, the trace records `coa.expand_chunks: unavailable` and the
answer is generated from the Knowledge Base evidence alone. Nothing leaks in
that case: an unavailable gate returns nothing, it does not return everything.

### What this does not change

The console path of ADR 0011 (COA retrieves as the user and answers) is
untouched; it is what a caller without an agent in front of it uses. The
semantic plan's `MANAGED_KB` directive is unchanged. The Knowledge Base's role,
the extraction identity, the chunk-id contract and the gate are all ADR 0011's.

## Alternatives considered

- **Trust the caller's chunk ids.** Rejected: a per-document ACL bypass for
  anyone who can guess or copy a content hash.
- **Return chunk text from the graph.** Rejected: the Knowledge Base already
  serves it under its ACL, and copying it out of COA would make COA a second
  text store with its own staleness and its own leak surface.
- **Make hop 2 cross documents unconditionally.** Rejected as the default:
  it costs one probe per reached document and reaches into documents the
  caller did not ask about. Available as `ENTITLED_DOCUMENTS`.
- **Seed `GraphTraverse` with chunk ids.** Rejected: that operation walks the
  published RDF ontology graph, authorized at namespace level and holding no
  document text; `expand_chunks` walks the per-document lexical graph. Two
  graphs, two gates, two operations.
- **Fail the whole agent request when expansion fails.** Rejected: the
  Knowledge Base evidence is complete and authorized on its own; refusing to
  answer would make COA's availability a dependency of the customer's agent
  for a leg that only adds context.

## Consequences

- An agent gets graph-aware document answers while COA never runs a search
  for it: COA sees chunk ids, not questions, on this path.
- One `expand_chunks` call per answered question in the outer agent, plus
  one Knowledge Base probe per seed document (cached 60 s per caller) and,
  under `ENTITLED_DOCUMENTS`, one per newly reached document.
- The id grammar and bounds now live in four places that must agree:
  `serve.smithy`, serve's handler, the data-layer `contract`, and the MCP
  execution layer; each has a test pinning it to the model.
- `withheld` is an honest signal to the agent that there is more it cannot
  see, without saying what. Agents should surface it ("the sources you have
  access to do not say who manages it") rather than hide it.

## Verification

Unit coverage, all local, at the three commits:

- serve: seed in a denied document is not expanded and the graph is not
  walked; an unknown seed is counted the same way; no caller means nothing;
  duplicate seeds count once; hop-1 facts carry Knowledge Base ids and the
  document; `SEED_DOCUMENTS` withholds the HR fact reached through "Project
  Atlas" from a finance reader and never probes the HR document;
  `ENTITLED_DOCUMENTS` probes it and still withholds it from that reader, and
  returns it to a reader of both; nothing withheld leaves a trace in the
  serialized response (document URI, chunk id, graph id, entity label, fact
  id all absent); `maxFacts` truncation is counted; a namespace without a
  Knowledge Base is not gated; the Cypher reaches the store fully formatted
  with tenant labels and ids as parameters; the handler rejects every
  out-of-contract member before the expander runs and never echoes the
  value; the admission gate denies before any graph read.
- data-layer and MCP: bounds and enum pinned to the model; out-of-contract
  members rejected at the edge; the operation is covered by the
  declared-member conformance test; `expand_chunks` gated under
  `searchDocuments`.
- outer agent: chunk ids taken from `MANAGED_KB` hits in rank order, deduped,
  one call's worth; `expand_chunks` called with the user's token, scope and
  budget; facts cited as `COA_GRAPH` evidence in one numbering with the hits;
  `OFF`, no chunk ids, and an unavailable graph each leave the plan as the
  only COA call and the trace says why.

### Deployed run, 2026-10-31 (dev deployment, `f4c4e5c`)

Stacks `api` (route + authorizer), `data-layer`, `serve`, `mcp`, `outer-agent`
redeployed; serve v16 and mcp v7 share one image, outer-agent v6, all `READY`;
`POST /namespaces/{id}/graph/expand` present on the REST API; a fresh MCP
session lists nine tools including `expand_chunks`. Fixture: namespace
`fmkb-acl-test`, a test Knowledge Base in eu-central-1 with one S3 data
source holding three documents (finance: alice + the caller below; HR and
shared: bob only; extractor: all three). The probe
(`sim/ppm/onboarding/coa_expand_probe.sh`) plays the agent: `Retrieve` on the
Knowledge Base as the signed-in user, then `expand_chunks` over MCP (and
`/graph/expand` over REST) with that user's Entra token.

| Step | Caller: a signed-in Entra user granted finance only | Result |
|---|---|---|
| `Retrieve` as the user, scoped to the data source | 1 chunk: `budget-2026.txt` (`Ukhx_oqNv…`) | the KB's own ACL; the unscoped KB also returns copies from other, un-ACL'd data sources, which is why serve scopes every call (ADR 0011) |
| `expand_chunks`, `SEED_DOCUMENTS` | 5 entities (Zafrilon, Crop Protection R&D, Seed Treatment Trials, Digital Agronomy, Reserve Buffer), 14 facts, all from `budget-2026.txt`; related 0; withheld 0/0/0; 1.1 s | hop 1 as designed |
| `expand_chunks`, `ENTITLED_DOCUMENTS` | identical | the three fixture documents share no entity (each was written with its own invented term), so hop 2 has nothing to reach and no probe fires |
| forged seed: the HR chunk id (`bnSIaOW…`), which this caller cannot retrieve | entities 0, facts 0; `withheld.seedChunks` 1 | the trust rule: the caller's claim is not believed |
| forged seed: the shared handbook chunk id | same | |
| unknown chunk id | same shape, 90 ms | unknown and denied are one count |
| REST `/graph/expand`, real seed | HTTP 200, 5 entities, 14 facts | same gate behind the API Gateway authorizer |
| REST, `chunkIds: ["bad id;DROP"]`, `scope: EVERYTHING` | HTTP 400 naming `chunkIds`, value not echoed | contract enforced at the edge |
| REST, forged HR seed | HTTP 200, facts 0, `withheld.seedChunks` 1 | |

What the run did not cover, and why:

- **The second reader and the cross-document cut.** The fixture's documents
  share no entity, so hop 2 is empty for every reader and the
  `SEED_DOCUMENTS` / `ENTITLED_DOCUMENTS` difference cannot show. The
  two-reader contrast is visible only through the forged-seed rows above. To
  observe the cut itself the fixture needs one entity named in two documents
  (for example a project named in both the finance and the HR file); that is
  an S3 upload the agent's host policy blocks (a human runs the fixture
  script), a Knowledge Base sync and a COA rescan. The second reader is the
  same person with a changed ACL, as in the ADR 0011 run, since one Entra
  identity is available.
- **The outer-agent leg** was not exercised on 2026-10-31 (Region, allowlist
  and profile gaps). It was on 2026-11-01; see the next section.

### Deployed run, 2026-11-01: the outer-agent leg end to end (`ffa65e9`)

Three things stood between the deployed agent (v6) and this fixture, none a
defect of `expand_chunks`: the namespace had no accepted ontology, so every
plan came back `AMBIGUOUS` with `ONTOLOGY_VERSION_UNVERIFIED` and zero
directives; the namespace profile named a `COA_GRAPH` target the agent has no
adapter for; and the agent's Bedrock client, IAM grant and target allowlist
were bound to the stack Region (us-east-1) while the fixture Knowledge Base is
in eu-central-1. Closing them:

1. **Ontology.** The ontology stack was redeployed on the T-Box-only induction
   (ADR 0011). `POST /induce` with `strategy: unstructured_lexical_graph` on
   the Knowledge Base source completed in 2 s: 1 class (`Process`), 2 datatype
   properties (`has capital allocation`, `duration`), 0 individuals, 0
   concepts, and the ADR 0011 warning on the proposal. Accepted; the live
   ontology is `induced-20261101T173723Z`.
2. **Profile.** `PUT /namespaces/{id}`: `ACCELERATE`, `profileVersion 2`,
   `ontologyVersion` set to that version, one target `knowledge-hub`
   (`MANAGED_KB`, `READ_ONLY`), `generationTarget portkey`.
3. **Target.** The deployment's `outerAgent.sourceTargets` gained
   `knowledge-hub` with `region eu-central-1`, `dataSourceIds` the fixture's
   data source, `reranking NONE` (the fixture is a managed-search Knowledge
   Base on a custom embedding model). The outer agent and its stack grew the
   matching knobs; the IAM grant now names the eu-central-1 KB ARN.

Four agent defects surfaced only once the leg ran, each fixed and covered by a
unit test on the recorded shape:

| Symptom (agent response) | Cause | Fix |
|---|---|---|
| `SemanticPlanUnresolved 409` against a plan COA returned as `RESOLVED` | FastMCP wraps a tool's text return in `structuredContent: {"result": "<json string>"}`; the client returned the envelope and the orchestrator found no `plan` in it | the single-key string envelope is parsed (`f825550`) |
| `InternalError 500`, no detail | botocore `ClientError` from `AgenticRetrieveStream` escaped the adapter | mapped to `SourceUnavailable 502` with the service's code and message (`73bf2f0`) |
| `ValidationException: Managed reranking is not supported for knowledge bases with custom embedding models` | `rerankingModelType MANAGED` hard-coded | per-target `reranking` (`MANAGED` \| `NONE`), default unchanged (`c901fd1`) |
| `ValidationError 400: Portkey response failed the grounded-answer contract` on a question the user's evidence cannot answer | the contract required at least one claim; the model honestly returned `{"claims": []}` | empty claims is the deterministic refusal, and the prompt asks for it (`ffa65e9`) |

The chain, as a signed-in Entra user granted finance only
(`sim/ppm/onboarding/coa_outer_agent_probe.sh`, outer-agent v10):

| Question | `coa.semantic_plan` | `sources.execute` | `coa.expand_chunks` | Answer |
|---|---|---|---|---|
| capital allocation for fiscal 2026 and its split | `RESOLVED`, ontology `induced-20261101T173723Z`, 1 directive `knowledge-hub`/`MANAGED_KB`, 14 s | 1 item: the finance chunk, 4 s | 1 seed, 14 facts, related 0, withheld 0/0/0, 15 s | two cited claims: 14.2 M EUR; 58 % crop protection R&D, 27 % seed treatment trials, 15 % digital agronomy. `[E1, E10, E11]`, `[E1, E4, E6, E8]`. 35 s total |
| duration of the onboarding process | same | same finance chunk (the only document this reader may see) | same 14 finance facts | "The authorized evidence does not support an answer to this question." |
| which process has a capital allocation for onboarding and leave policies in HR | same | same | same | same refusal |

Nothing from `hr/` or `shared/` reached the evidence or the answer on any of
the three questions; the refusal is the agent's, from the model's empty claim
list, with the finance evidence still listed in the response for audit.

Two boundaries this run made visible, both upstream of `expand_chunks`:

- **Planning is gated on NL-to-SPARQL.** `semantic_execution_plan` returns
  `400 Could not generate a semantic source directive` when the translator
  yields no SPARQL, which happens when the question names nothing in the
  namespace ontology ("Who is the HR contact and what are the leave and
  onboarding policies?" against a vocabulary of `Process`, `has capital
  allocation`, `duration`). A question on a document domain whose vocabulary
  is not in the ontology never reaches the Knowledge Base. The agent reports
  this as `SemanticPlanUnresolved`; the richer the T-Box, the fewer the
  refusals. Not changed here.
- **The second reader.** Still the same person with a changed ACL; the fixture
  grant is an S3 upload the agent's host policy blocks, so the HR-side
  observation is run by a human (`coa_kb_fixture_add_user.sh <email> hr`,
  then the probe again). The cross-document cut still needs an entity shared
  by two documents, as noted above.

Scripts: `sim/ppm/onboarding/deploy_agent_leg.sh` (outer-agent + ontology
stacks, asserts the SSM target reaches the runtime), `ssm_add_kb_target.py`
(idempotent allowlist edit), `coa_outer_agent_probe.sh` (the chain as the
signed-in user, prints trace, plan, evidence and answer).
