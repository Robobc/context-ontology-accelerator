# ADR 0012: Let an agent that retrieved from the Knowledge Base itself pivot into COA's graph

## Status

Proposed. Implemented on `feature/bedrock-kb-chunk-source` in three layers,
each committed on its own: serve (`GraphExpandChunks` on the data-layer
surface, `tier3/chunk_expander.py`), the REST edge and the `expand_chunks` MCP
tool, and the outer agent's chaining of `Retrieve` into `expand_chunks`. Not
yet deployed; the deployed-run record is to be added under Verification once
it has run against the ACL fixture of ADR 0011.

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

Deployed run against the ADR 0011 fixture (`fmkb-acl-test`, Knowledge Base
`71RZ44THV5`): not yet done. The observation to record is the two-reader case
from the ADR 0011 run, driven through the outer agent: the finance reader's
answer carries the budget facts and a `withheld` count for HR; the HR reader's
answer carries the manager and a `withheld` count for finance; neither answer
names the other's document.
