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

**serve, graph.** A `DocumentEntitlementGate` applies to every namespace routed
to a per-user Knowledge Base. Before a tool returns Neptune-held text (chunk
window expansion, graph traversal), the gate resolves the provenance of each
item to its source documents (`chunk -> source`, or
`entity -> fact -> statement -> chunk -> source`), asks the Knowledge Base per
document whether the caller may read it (`Retrieve`, `numberOfResults=1`,
filtered on `_source_uri`, with `userContext`; non-empty means entitled), and
drops anything unentitled or whose provenance cannot be resolved. Answers are
cached per `(user, namespace, uri)` for 60 s. Entities keep their structure;
only their statement text is withheld. Chunks not minted from the Knowledge
Base (ids without `aws:`) are not under its ACL and pass through.

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
| `BEDROCK_KB_ENFORCE_USER_CONTEXT` | `false` | `true` puts the caller on every `Retrieve` and `entitled_source_uris` probe; no caller, no results. Also enables the `DocumentEntitlementGate` for routed namespaces. |

## Verification

Unit: `uv run pytest packages/sources/tests/unit/documents/kg_build/test_bedrock_kb_loader.py packages/context-manager/tests/unit/test_bedrock_kb_entitlement.py packages/context-manager/tests/unit/test_bedrock_kb_retriever.py -q`.
Pinned behaviours: `aws:` ids survive the toolkit; chunk count equals distinct
`Retrieve` results; `chunking=None` under `BEDROCK_KB`; `dummy://` under
`DUMMY`; a caller without entitlement sees nothing from an ACL data source, an
entitled caller sees only their documents, and a request with no caller sees
nothing.

Deployed (pending): against a Knowledge Base with a document-level ACL data
source, build the graph, then query as two users with different entitlements
and confirm the traversal output contains no statement text from documents
the user cannot `Retrieve`. This is the observation step for the gate.
