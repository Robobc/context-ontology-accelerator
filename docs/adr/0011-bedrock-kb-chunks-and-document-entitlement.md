# ADR 0011: Build the lexical graph from Bedrock Knowledge Base chunks and gate graph text by document entitlement

## Status

Proposed. Implemented on `feature/bedrock-kb-chunk-source` (kg-build loader,
serve retriever, entitlement gate); the source sub-type, IAM and a deployed
run against a real Knowledge Base are pending. Supersedes the last consequence
of ADR 0003 ("per-document ACL-to-retrieval-filter translation is not yet
wired").

## Context

ADR 0003 made a Bedrock Managed Knowledge Base the document retriever for a
namespace, with one restriction: the whole corpus had to be authorized for
every caller, because retrieval ran without a user identity and COA's graph
held text extracted from every document.

The customer platform this fork targets holds hundreds of Managed Knowledge
Bases over SharePoint with document-level ACL and verified Entra identity.
Re-ingesting those files into COA's own pipeline would duplicate parsing,
chunking and embedding, and would discard the ACL. The only way to add a graph
layer without a second copy of the corpus is to build it from the chunks the
Knowledge Base already holds, and to make every graph read answerable to the
Knowledge Base's own authorization.

Facts the design depends on, verified against the pinned toolkit
(`graphrag-lexical-graph` 3.18.7) and a live Managed Knowledge Base:

- `Retrieve` returns `metadata._chunk_id` per result, stable across syncs and
  content-derived; there is no chunk enumeration API and
  `ListKnowledgeBaseDocuments` rejects SharePoint data sources.
- `Retrieve` with `userContext.userId` enforces document-level ACL; without it
  an ACL-enabled data source returns nothing.
- The toolkit rehashes every node id unless it starts with `aws:`, merges
  `__Chunk__` nodes on that id, and only skips its own splitter when
  `IndexingConfig(chunking=None)` is passed explicitly.

## Decision

**kg-build.** `INPUT_MODE=BEDROCK_KB` loads a Knowledge Base's chunks through a
document-scoped `Retrieve` scan (documents from `ListKnowledgeBaseDocuments`
where the data source allows it, otherwise discovered through `Retrieve`
sampling) and hands them to the toolkit as pre-chunked `TextNode`s with
`id_ = "aws:" + _chunk_id` and a `SOURCE` relationship to
`"aws:doc:" + documentId`. Chunking is `None`; nothing is re-split. Raw ids are
kept as `bedrockChunkId` / `bedrockDocumentId` properties so joins never
depend on prefix stripping. `VECTOR_STORE_MODE=DUMMY` leaves chunk and topic
embeddings in the Knowledge Base instead of writing them to OpenSearch.
Extraction runs under a dedicated role **without** `userContext`, so the graph
contains facts from every document. The default mode (`STAGING`, `AOSS`) is
unchanged.

**serve, retrieval.** The existing `BedrockKnowledgeBaseRetriever` emits chunk
ids as `aws:<_chunk_id>`, so a vector hit is the id of the graph node and no
translation table exists. With `BEDROCK_KB_ENFORCE_USER_CONTEXT=true` every
`Retrieve` carries the caller as `userContext.userId`; a request with no
resolvable caller returns no chunks. The caller is published once per request
at the admission gate through a `ContextVar` rather than threaded through every
retriever and tool signature, because those signatures are upstream code that
changes each release.

**serve, graph.** One `DocumentEntitlementGate`, built at startup and shared,
applies to every namespace routed to a per-user Knowledge Base and to every
reader of the lexical graph: the agentic graph-traversal, chunk-window and
vector-search tools, the agentic `strategy:*` tools and the lexical-baseline
Tier-3 path (the last two through the one `BaselineLexicalRetriever` adapter
they share, which gates result nodes by source document and then the entity
labels those nodes aggregate from other documents).
Before any graph-derived content is returned, the gate resolves its provenance
to source documents (`chunk -> source`, `source id -> uri` for toolkit result
nodes, or `entity -> fact -> statement -> chunk -> source` for statement text
and entity labels), asks the Knowledge Base per document whether the caller
may read it (`Retrieve`, `numberOfResults=1`, filtered on `_source_uri`, with
`userContext`; non-empty means entitled), and drops anything unentitled or
whose provenance cannot be resolved. Entity labels are document-derived text
and are withheld like statements, together with the planner directions built
from them. Definite answers are cached per `(user, namespace, uri)` for 60 s in
a bounded cache; a probe the Knowledge Base could not answer (throttled,
unavailable) withholds the document for that request only and is not cached,
so an outage never becomes a remembered denial. Content not minted from the
Knowledge Base (chunk ids without `aws:`, source ids without `aws:doc:`) is not
under its ACL and passes through, logged.

**serve, request scope.** The `ContextVar` identity is cleared when the
entrypoint starts a request and restored when it ends (`try/finally` around the
handler), so a request that skips the admission gate (session CRUD,
`semanticPlan`, `translate`) on a reused worker never reads the previous
caller.

**serve, ontology graph.** The SPARQL ontology graph read by `graphTraverse`
and the non-agentic `KnowledgeRetriever` is **not** gated per document. It is
authorized at namespace level by the Cedar admission gate, and it holds induced
classes and properties, not document text. That holds only as long as
unstructured induction is **not** run over a per-user Knowledge Base's lexical
graph (it would promote document-derived individuals into the ontology). This
is enforced, not assumed: the graph declares its own origin (KB documents are
`__Source__` nodes with ids `aws:doc:*`), and the unstructured induction worker
checks `LexicalGraphStore.has_access_controlled_sources()` right after
connecting; a positive answer fails the job before any LLM call and creates no
proposal (`AccessControlledGraphError`). No second source of truth is needed.

**Flag coupling.** `BEDROCK_KB_SEARCH_MODE=managed` implies
`BEDROCK_KB_ENFORCE_USER_CONTEXT=true` unless set to `false` explicitly, and
that opt-out is logged at startup: a Managed Knowledge Base with document ACL
queried without a caller would return the extraction role's view to everyone.

**Not decided here.** Chunk text remains in Neptune as the toolkit writes it;
it is treated as a cache of the Knowledge Base's text and is reachable only
through the gate. Removing it is a possible later hardening. Incremental
re-extraction after a Knowledge Base sync (diff on `_chunk_id` sets) is a
separate milestone.

## Consequences

- A namespace can gain a graph layer over a corpus it does not own a copy of;
  parsing, chunking and embedding stay with the Knowledge Base.
- Authorization has one source of truth, the Knowledge Base. COA never
  evaluates an ACL itself; it asks, per document and per request, and fails
  closed on every error, missing identity or unresolved provenance.
- Cost: one `Retrieve` per candidate document per request (bounded by the
  60 s cache), on top of the retrieval call. Topic embeddings do not exist
  under `DUMMY`, so Tier-3 topic retrieval is unavailable for such namespaces.
- `_chunk_id` stability and the `aws:` passthrough are observed behaviour, not
  documented contracts; both are pinned by unit tests and must be re-checked
  on every toolkit or Bedrock change. Detected id churn means a full rebuild.
- `graph_build.py` and the agentic tool wiring are upstream files; the hooks
  there are kept to a few lines each so release rebases stay cheap.
- Under the `STAGING` input mode and without a per-user route, nothing in this
  decision is active and behaviour is byte-identical to upstream.

## Configuration

kg-build task environment (`packages/sources/.../kg_build/graph_build.py`):

| Variable | Default | Meaning |
|---|---|---|
| `INPUT_MODE` | `STAGING` | `BEDROCK_KB` loads the Knowledge Base's chunks; anything else keeps the staged-file path. |
| `VECTOR_STORE_MODE` | `AOSS` | `DUMMY` writes no embeddings (chunk vectors stay in the Knowledge Base). |
| `BEDROCK_KB_ID` | required when `INPUT_MODE=BEDROCK_KB` | Knowledge Base to read. |
| `BEDROCK_KB_DATA_SOURCE_IDS` | empty = all | Comma-separated data source ids to scope the scan. |
| `BEDROCK_KB_REGION` | `AWS_REGION` | Region of the Knowledge Base. |
| `BEDROCK_KB_EXTRACTION_ROLE_ARN` | empty = task role | Role assumed for the Bedrock calls; must see every document. |
| `CHUNK_SIZE` | toolkit default | Ignored under `BEDROCK_KB` (logged). |

serve environment (`packages/context-manager/src/coa_serve/config.py`),
in addition to `DOCUMENT_RETRIEVER_BACKEND`, `BEDROCK_KNOWLEDGE_BASE_ID` and
`BEDROCK_KB_BY_NAMESPACE` from ADR 0003:

| Variable | Default | Meaning |
|---|---|---|
| `BEDROCK_KB_SEARCH_MODE` | `vector` | `managed` sends `managedSearchConfiguration`, required for Managed Knowledge Bases. |
| `BEDROCK_KB_ENFORCE_USER_CONTEXT` | `true` when `BEDROCK_KB_SEARCH_MODE=managed`, else `false` | `true` puts the caller on every `Retrieve` and entitlement probe; no caller, no results. Also enables the `DocumentEntitlementGate` for routed namespaces. An explicit `false` under `managed` is logged. |

## Verification

Unit: `uv run pytest packages/sources/tests/unit/documents/kg_build/test_bedrock_kb_loader.py packages/context-manager/tests/unit/test_bedrock_kb_entitlement.py packages/context-manager/tests/unit/test_bedrock_kb_entitlement_e2e.py packages/context-manager/tests/unit/test_bedrock_kb_retriever.py -q`.
Pinned behaviours: `aws:` ids survive the toolkit; chunk count equals distinct
`Retrieve` results; `chunking=None` under `BEDROCK_KB`; `dummy://` under
`DUMMY`; a caller without entitlement sees nothing from an ACL data source, an
entitled caller sees only their documents, and a request with no caller sees
nothing. The `_e2e` file goes through the real builders
(`build_document_entitlement_gate`, `build_tool_registry`,
`build_lexical_retriever`) with one fake lexical graph and keeps a control case
per path that returns the restricted text when the gate is left out, so the
assertions fail if the gate is unwired, not only if the fake changes. The
entrypoint identity scope is asserted directly (stale caller invisible on
entry, restored on exit, also when the body raises).

Review: an independent review of the first gate commit found the request-scope
leak, the ungated lexical path, the entity-label leak and the mocked wiring
tests; all four are closed above and the end-to-end file was the response to
the last one. A second independent review of the fixed state returned no
blocker and two majors, both closed: entity labels the lexical path aggregates
from a node's `entity_contexts` (which span documents) are now gated per entity,
and the induction suppression is enforced in the worker rather than asserted.
It also had the agentic `vector_search` tool gated (the OpenSearch chunk index
holds every document's text under `VECTOR_STORE_MODE=AOSS`), a `chunk_window`
end-to-end control added, and the `main.py` wiring pinned statically.

Deployed observation step (must pass before the gate is trusted live), as the
second review phrased it:

1. A known **entitled** document round-trips: the graph's
   `__Source__.source_uri` must byte-equal the `_source_uri` value the
   Knowledge Base filters on (encoding, case, trailing slash). Assert an
   entitled document probes `True`, not only that a denied one probes `False`;
   a mismatch fails closed for every document and looks like an outage.
2. The probe query (`"document"`, `numberOfResults=1`, filtered to one
   document) returns a hit for an entitled document that contains nothing
   relevant to that word. If it does not, the probe must move to a content-free
   check before GA.

Deployed (pending): against a Knowledge Base with a document-level ACL data
source, build the graph, then query as two users with different entitlements
and confirm the traversal output contains no statement text from documents
the user cannot `Retrieve`. This is the observation step for the gate.
