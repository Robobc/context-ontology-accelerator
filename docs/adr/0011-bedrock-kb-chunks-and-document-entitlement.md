# ADR 0011: Build the lexical graph from Bedrock Knowledge Base chunks and gate graph text by document entitlement

## Status

Proposed. Implemented on `feature/bedrock-kb-chunk-source`: kg-build loader,
serve retriever and entitlement gate, the `BEDROCK_MANAGED_KB` source sub-type
with its IAM, registry-based routing in serve, the extraction identity, and
the Connect Source wizard tab. Deployed to the dev deployment and run against a
real Managed Knowledge Base with an ACL data source on 2026-10-31 (results
under Verification); the per-user entitlement held end to end for one real
user in two ACL states. Supersedes the last consequence of ADR 0003
("per-document ACL-to-retrieval-filter translation is not yet wired") and
ADR 0003's deployment-level namespace-to-Knowledge-Base routing. Extended by
ADR 0012, which adds the entry point for an agent that runs `Retrieve` itself
and hands COA the chunk ids to expand (the query path below stays the console's
COA-driven one).

The guiding decision, taken with the customer on 2026-10-30: **a Knowledge
Base is a source like any other.** It is registered, trusted, scanned,
re-scanned, listed and routed exactly as a Databricks, Glue or JDBC source is,
and nothing about it lives in deployment configuration. The two places it is
more than a connector are internal: what a scan does (extraction only, over the
Knowledge Base's own chunks) and what a query does (the vector leg goes to the
Knowledge Base as the asking user, and the graph leg is gated to the same
answer).

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
(`graphrag-lexical-graph` 3.18.7) and a live Managed Knowledge Base
(a test Managed KB in eu-central-1, 2026-10-25 and 2026-10-30):

- `Retrieve` returns `metadata._chunk_id` per result, stable across syncs and
  content-derived (observed; the service team has been asked whether it is
  guaranteed); there is no chunk enumeration API and
  `ListKnowledgeBaseDocuments` rejects SharePoint data sources. For an S3 data
  source the `documentId` is the `s3://` URI while `_source_uri`, the key the
  KB filters on, is the `https://` form of the same object.
- `Retrieve` and `GetDocumentContent` with `userContext.userId` enforce
  document-level ACL. **Without a user context an ACL-enabled data source
  returns nothing** (`GetDocumentContent` refuses with "UserContext is
  required for ACL-aware data sources"), a document with no ACL is restricted
  rather than public, and **there is no administrative or service identity
  that sees everything** ([docs](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-retrieve-acl.html)).
  Non-ACL data sources return every document to any identity.
- The toolkit touches node ids in two stages. The extract stage's
  `IdRewriter._new_id` leaves any id starting with `aws:` alone. The build
  stage's `NodeBuilders.pre_process` then applies `TenantId.rewrite_id` to
  every node id and relationship id: `parts = id.split(":")`, result
  `f"{parts[0]}:{tenant}:{':'.join(parts[2:])}"`. Segment 1 is replaced by the
  tenant; an id with only two segments loses its tail (`aws:<x>` becomes
  `aws:<tenant>:`, every chunk onto one node). The toolkit merges `__Chunk__`
  nodes on the stored id and only skips its own splitter when
  `IndexingConfig(chunking=None)` is passed explicitly.

## Decision

**kg-build.** `INPUT_MODE=BEDROCK_KB` loads a Knowledge Base's chunks through a
document-scoped `Retrieve` scan (documents from `ListKnowledgeBaseDocuments`
where the data source allows it, located in the data-source scan by
`documentId` and read through their `_source_uri`; otherwise discovered through
`Retrieve` sampling) and hands them to the toolkit as pre-chunked `TextNode`s.
Chunking is `None`; nothing is re-split. `VECTOR_STORE_MODE=DUMMY` leaves chunk
and topic embeddings in the Knowledge Base instead of writing them to
OpenSearch. The default mode (`STAGING`, `AOSS`) is unchanged.

**Id contract** (`libs/common/src/coa_common/kb_ids.py`, the one place the
shape is written; kg-build, serve and the ontology engine import it). Because
the toolkit rewrites segment 1 of every id to the tenant, ids are emitted in
the toolkit's own shape with an empty tenant slot and stored with it filled:

| | emitted by kg-build | stored in the graph |
|---|---|---|
| `__Chunk__` | `aws::kb:<_chunk_id>` | `aws:<tenant>:kb:<_chunk_id>` |
| `__Source__` | `aws::doc:<tail>` | `aws:<tenant>:doc:<tail>` |

`<tenant>` is `to_graphrag_tenant_id(namespace)`, which serve knows, so a
`Retrieve` hit's `_chunk_id` is turned into the exact stored node id with no
lookup (`stored_chunk_id`). The `kb` / `doc` markers at segment 2 are what
distinguish Knowledge Base content from the toolkit's own nodes
(`aws:<tenant>:<hex8>:<hex4>...`), which are not under any document ACL. The
document tail is the `documentId` with `#` -> `_` when that fits the id
grammar (SharePoint's `4#<uuid>#<sha256>`), else `h.<sha256[:32]>` (an S3
`documentId` is an `s3://` URI); the raw ids stay on the nodes as
`bedrockChunkId` / `bedrockDocumentId`, and the gate resolves a source to its
`_source_uri` through a property, never by parsing the id. A unit test runs
the toolkit's real `TenantId.rewrite_id` on the emitted ids so the two halves
cannot drift again; the first implementation had emitted `aws:<_chunk_id>`,
verified only against the extract stage, and would have collapsed every chunk
onto one node while stripping the prefix the gate tested for.

**Extraction identity.** The scan reads the Knowledge Base as one identity,
`bedrockKnowledgeBaseConfiguration.extractionUserId`, passed as
`userContext.userId` on every `Retrieve` and `GetDocumentContent`: the email of
a user or service account the Knowledge Base owner has granted read access to
every document the graph should cover. There is no other way to read an
ACL-enabled data source, so **the graph's coverage is exactly that identity's
entitlement**, set by the owner in the source system (for SharePoint, a
service account with read permission on the sites in scope, checked by
Bedrock against the synced ACLs and in real time). The identity decides
coverage, never visibility: every question is still answered as the asking
user. A scan over a data source that lists documents the identity cannot read
at all fails the job (`ExtractionIdentityRequiredError`, surfaced on the
source row naming the field) instead of building an empty graph that looks
like a successful one; partial visibility is counted and logged
(`documents_not_visible`). Non-ACL data sources ignore the identity. Optional
on the API for that reason; required in practice for any ACL-enabled source.

**serve, routing.** Which Knowledge Base serves a namespace is read from the
namespace's source registry (the same DynamoDB inventory Tier-2 routes
Databricks and Glue sources from), never from deployment configuration. The
admission gate resolves the namespace's `BEDROCK_MANAGED_KB` source once per
request (`SourcesRegistry.find_bedrock_kb_source`) and binds a per-user
retriever for it to the request (`request_document_source`, the same
`ContextVar` pattern as the caller identity); the document retriever and the
entitlement gate read that binding. A namespace with no such source is served
from COA's own index as before. A namespace whose inventory cannot be read, or
which registers **more than one** Knowledge Base source (`Retrieve` takes one
`knowledgeBaseId`; this release does no fan-out), or whose cross-account assume
is refused, is **not served**: falling back would answer from an OpenSearch
index holding none of its chunks with the gate off. Retrievers are built on
first use and cached per source (`KbRetrieverCache`); a source in another
account is read through a refreshable session assumed from its
`crossAccountRoleArn` with the namespace ExternalId
(`refreshable_datasource_session`, serve is long-lived), a same-account source
under the serve runtime's own role.

**serve, retrieval.** The existing `BedrockKnowledgeBaseRetriever` emits chunk
ids as `stored_chunk_id(tenant, _chunk_id)` = `aws:<tenant>:kb:<_chunk_id>`,
so a vector hit is the id of the graph node and no translation table exists. A Managed Knowledge Base source is **always** queried
per user: every `Retrieve` carries the caller as `userContext.userId` with
`managedSearchConfiguration`, and a request with no resolvable caller returns
no chunks. There is no switch to turn this off; the former
`BEDROCK_KB_SEARCH_MODE` / `BEDROCK_KB_ENFORCE_USER_CONTEXT` knobs are gone,
because a Managed Knowledge Base with document ACL queried without a caller
would return the extraction role's view to everyone. The caller is published
once per request at the admission gate through a `ContextVar` rather than
threaded through every retriever and tool signature, because those signatures
are upstream code that changes each release.

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
Knowledge Base (ids without the `kb` / `doc` marker, `is_kb_chunk_id` /
`is_kb_source_id`) is not under its ACL and passes through, logged.

**serve, request scope.** The `ContextVar` identity is cleared when the
entrypoint starts a request and restored when it ends (`try/finally` around the
handler), so a request that skips the admission gate (session CRUD,
`semanticPlan`, `translate`) on a reused worker never reads the previous
caller.

**serve, ontology graph.** The SPARQL ontology graph read by `graphTraverse`
and the non-agentic `KnowledgeRetriever` is **not** gated per document. It is
authorized at namespace level by the Cedar admission gate, and it holds induced
classes and properties, not document text. That holds only as long as
unstructured induction over a per-user Knowledge Base's lexical graph emits
**schema terms only**. The graph holds every document's entities under one
tenant while the Knowledge Base authorizes per user, so the inducer's
entity-level shapes would leak restricted documents into every reader's view:
Rule 2 (`owl:NamedIndividual` from sampled entities), Rule 5 (`skos:Concept`
from `__Topic__` nodes, which are LLM summaries of chunk text) and the
`label_only` / `instance` description modes (sampled entity labels sent to the
LLM, listed verbatim by the fallback). Rules 1, 3 and 4 and the `schema`
description mode emit class names, predicate names, domains, XSD ranges and
observation counts; they read entity data but write none of it.

This is enforced, not assumed: the graph declares its own origin (KB documents
are `__Source__` nodes with ids `aws:<tenant>:doc:*`, tested with the shared
`KB_SOURCE_ID_CYPHER_PREDICATE`), and the unstructured induction worker probes
`LexicalGraphStore.has_access_controlled_sources()` right after connecting and
threads the answer into the pipeline config as `tbox_only`. On such a run the
pipeline does not read topics and skips Rule 5, and it refuses (not downgrades)
a request for `include_individuals=true` or a description mode other than
`schema` before any store or LLM call (`AccessControlledGraphError`; the job
fails, no proposal). The web console's request already uses those defaults, so
the Ontology page's "induce" button works on a Knowledge Base namespace and
yields classes and properties. Every T-Box-only proposal carries the
`TBOX_ONLY_WARNING` string in its `warnings` so the steward knows which shapes
were withheld. Class and predicate *names* still come from the corpus; a class
named after a restricted subject reveals that the subject exists. That is the
same exposure as the serve-side ontology graph already accepts at namespace
level, and it is the line this ADR draws: terms yes, instances no.

Not built, kept as the next lever if the customer wants individuals or topics for
deep-reasoning steering: an induction identity (`inductionUserId`, defaulting
to `extractionUserId`) with a provenance filter in the lexical stores, so the
full rule set runs over exactly the documents that identity may read.

**Registration (`BEDROCK_MANAGED_KB` sub-type).** A document source carrying
`bedrockKnowledgeBaseConfiguration` (`knowledgeBaseId`, optional
`dataSourceIds`, `region` and `crossAccountRoleArn`) is the sub-type; it is
mutually exclusive with `sourceBucketArn`, `uploadId`, `s3Prefixes` and
`roleArn`, and stages no files (`s3Prefixes` is stored empty). In the web
console it is the third tab of the Documents source kind, beside Upload files
and S3 bucket, with the same form the Databricks step uses for its role: the
reserved-name rule stated with the deployment's real prefix (derived from the
namespace's ExternalId) and the ExternalId shown read-only, never typed.

`crossAccountRoleArn` is the **same member, with the same meaning, as on the
Glue, JDBC and Databricks configurations**, and optional as on Glue: omitted,
the Knowledge Base is in the deployment's own account and is read directly
under the kg-build task role and the serve runtime role, which hold an
in-account `bedrock:Retrieve` / `GetDocumentContent` /
`ListKnowledgeBaseDocuments` grant on `knowledge-base/*`. Given, it is a
customer-owned role in the Knowledge Base's account and is treated exactly like
the Databricks credential-access role: named `{prefix}-datasource-access-*` at
the root IAM path in the `aws` partition (checked at submit with the same
helpers, so a role neither consumer could ever assume is refused with the
prefix named rather than failing the first extraction), and assumed with
`ExternalId = datasource_external_id(namespace)`, the value the control plane
shows on `GetNamespace`. The role ARN arrives from the API caller; the
namespace-derived ExternalId is what binds the assume to the namespace entitled
to it (confused deputy). **Two** COA principals assume it, so the role's trust
policy must name both: the kg-build task (extraction, which must see every
document) and the serve runtime (per-user `Retrieve` and the entitlement
probe). They are the fourth and fifth consumers of the one scoped
`sts:AssumeRole` grant shape (`arn:aws:iam::*:role/{prefix}-datasource-access-*`,
ExternalId required); serve's copy lives in a fork-owned helper
(`infra/lib/utils/datasource-assume.ts`) that its stack test pins byte-equal to
the sources stack's. Both principals are published in SSM
(`/{project}/{env}/sources/doc-kg-build-role-arn`,
`/{project}/{env}/serve/runtime-role-arn`) so the Knowledge Base owner reads
them rather than assembling them.

**Governance.** Four decisions, three owners, none of them deployment config:
*which* Knowledge Bases a namespace may read is decided by the Knowledge Base
owner, in their account, through the role's trust policy (which namespaces, by
ExternalId) and permission policy (which Knowledge Bases); *which documents
the graph covers* is decided by the same owner, in the source system, through
what the extraction identity may read; *that* a namespace reads a Knowledge
Base is decided by whoever holds `manageSource` on that namespace, by
registering it; *what each user sees* is decided by the Knowledge Base's own
document ACL on every `Retrieve`, and the graph is gated to the same answer.
One consequence worth stating: Bedrock trusts the calling role to assert the
`userId`, so whoever may call `Retrieve` on a Knowledge Base through COA's
roles may read as anyone; the role, not the identity string, is the boundary,
which is why the roles are COA's and the ExternalId binds them to a namespace.
One role can serve several namespaces by listing their ExternalIds, so a
Knowledge Hub with hundreds of Knowledge Bases needs one role per owning
account, not one per Knowledge Base or per namespace.

**Pipeline.** The ingestion state machine routes on `input_mode`: `BEDROCK_KB`
bypasses preprocessing (which lists S3 and would report the source empty) via a
`Pass` that supplies the `staging_prefix` the shared task definition still
binds; every other value, and an input predating the field, takes the
unchanged staging path. The trigger Lambda always emits the flat `input_mode`,
`vector_store_mode` and `bedrock_kb_*` string fields (staging defaults when the
job carries no `bedrock_kb`), because the task environment binds them with
`JsonPath.stringAt`, which fails the execution on a missing path. Re-scan
re-extracts from the Knowledge Base by forwarding the stored configuration.

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
- `_chunk_id` stability is observed behaviour, not a documented contract, and
  the toolkit's two-stage id handling is an implementation detail; both are
  pinned by unit tests (the second with the toolkit's real `rewrite_id`) and
  must be re-checked on every toolkit or Bedrock change. Detected id churn
  means a full rebuild.
- The Knowledge Base owner must provision one more thing than for Databricks:
  an extraction identity with read access to the documents in scope. For
  SharePoint that is an Entra account with site read permission; Bedrock's
  real-time ACL check applies to it like to any user.
- `graph_build.py` and the agentic tool wiring are upstream files; the hooks
  there are kept to a few lines each so release rebases stay cheap.
- A namespace without a `BEDROCK_MANAGED_KB` source is served exactly as
  upstream serves it; nothing in this decision is active and no deployment
  setting can switch it on. A namespace with one is served only through its
  Knowledge Base, per user, or not at all.
- The deployment config carries nothing per Knowledge Base. Adding a Knowledge
  Base is a registration (API or wizard); connecting a Knowledge Base account is
  one role in that account. Neither needs a deploy.
- A namespace holds at most one Knowledge Base source. Registering a second
  makes the namespace unservable (the admission gate refuses) until one is
  deleted; the alternative, picking one, would silently hide the other's
  documents. Serving several Knowledge Bases per namespace is a later
  milestone.

## Configuration

kg-build task environment (`packages/sources/.../kg_build/graph_build.py`):

| Variable | Default | Meaning |
|---|---|---|
| `INPUT_MODE` | `STAGING` | `BEDROCK_KB` loads the Knowledge Base's chunks; anything else keeps the staged-file path. |
| `VECTOR_STORE_MODE` | `AOSS` | `DUMMY` writes no embeddings (chunk vectors stay in the Knowledge Base). |
| `BEDROCK_KB_ID` | required when `INPUT_MODE=BEDROCK_KB` | Knowledge Base to read. |
| `BEDROCK_KB_DATA_SOURCE_IDS` | empty = all | Comma-separated data source ids to scope the scan. |
| `BEDROCK_KB_REGION` | `AWS_REGION` | Region of the Knowledge Base. |
| `BEDROCK_KB_CROSS_ACCOUNT_ROLE_ARN` | empty = task role | `{prefix}-datasource-access-*` role assumed with the namespace ExternalId for the Bedrock calls (IAM side). |
| `BEDROCK_KB_EXTRACTION_USER_ID` | empty = no user context | `userContext.userId` for every `Retrieve` / `GetDocumentContent` of the scan (ACL side); what it may read is what the graph contains. Empty against an ACL-enabled data source fails the job. |
| `RESOURCE_PREFIX` | set by CDK | The `{prefix}-{env}-` the ExternalId is derived from; the task fails the assume rather than default it. |
| `CHUNK_SIZE` | toolkit default | Ignored under `BEDROCK_KB` (logged). |

The state machine sets the first seven from the trigger Lambda's flattened job
input; none is operator-set. The source-level configuration is the API's
`bedrockKnowledgeBaseConfiguration` (Smithy `unified-sources.smithy`).

serve environment (`packages/context-manager/src/coa_serve/config.py`): **no
Knowledge Base setting.** Which Knowledge Base, in which Region, through which
role, is read from the registered source at request time. The former ADR 0003
variables `DOCUMENT_RETRIEVER_BACKEND`, `BEDROCK_KNOWLEDGE_BASE_ID` and
`BEDROCK_KB_BY_NAMESPACE`, and this ADR's earlier `BEDROCK_KB_SEARCH_MODE` and
`BEDROCK_KB_ENFORCE_USER_CONTEXT`, are removed together with their SSM
deployment-config fields (`documentRetrieverBackend`, `bedrockKnowledgeBaseId`,
`bedrockKnowledgeBaseByNamespace`, `bedrockKbSearchMode`,
`bedrockKbEnforceUserContext`); stale values in an existing config are ignored,
not rejected. Serve keeps one fork variable:

| Variable | Default | Meaning |
|---|---|---|
| `RESOURCE_PREFIX` | set by CDK | The `{prefix}-{env}-` the ExternalId is derived from, the same value the kg-build task gets; serve fails the assume rather than default it. |

Published for the Knowledge Base owner (SSM, both env-scoped because they
become IAM trust grants in another account):

| Parameter | Value |
|---|---|
| `/{project}/{env}/sources/doc-kg-build-role-arn` | kg-build ECS task role; extraction-time principal. |
| `/{project}/{env}/serve/runtime-role-arn` | serve runtime role; query-time principal (already published upstream). |

The customer-facing recipe (role name, trust policy with both principals and
the ExternalId condition, permission policy) is in
`external-docs/content/sources.md`, "Bedrock Managed Knowledge Base Source".

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
and the induction restriction is enforced in the pipeline rather than asserted
(first as a blanket refusal in the worker, then narrowed to T-Box only once the
per-rule leak surface was traced; see the ontology-graph paragraph above).
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
3. Registration end to end, through the wizard: Documents, "Bedrock Knowledge
   Base" tab, a `{prefix}-datasource-access-*` role in the Knowledge Base's
   account whose trust policy names **both** the kg-build task role and the
   serve runtime role with the namespace's `datasourceExternalId` (or no role,
   for a same-account Knowledge Base); the ingestion execution takes the
   `SourcesBedrockKbSkipPreprocess` branch, the task assumes the role
   (CloudTrail in the KB account shows the `coa-kg-build-<namespace>` session
   with the ExternalId), and the source reaches `COMPLETED` with `chunksGraph`
   equal to the Knowledge Base's distinct `Retrieve` result count for the
   scoped data sources.
4. Routing: a query on the namespace reaches the Knowledge Base as the caller
   (CloudTrail `Retrieve` with the serve session and `userContext`) with no
   Knowledge Base setting in the serve environment; a namespace without a
   Knowledge Base source on the same deployment is served from COA's own
   index unchanged; registering a second Knowledge Base source on the
   namespace makes queries fail with `DataSourceUnavailableError` until one
   is deleted.
5. Chunk-id stability: a second scan of the same Knowledge Base produces the
   same `_chunk_id` set (zero drift), which is the assumption the mapping
   rests on. The service team has been asked whether it is guaranteed.
6. Id contract live: after the build, `MATCH (c:__Chunk__) RETURN id(c)` on
   the namespace's graph shows `aws:<tenant>:kb:<_chunk_id>` ids whose count
   equals the distinct `Retrieve` results (no collapse), and a `Retrieve` hit's
   `stored_chunk_id` resolves to one node.
7. Extraction identity: a scan of an ACL-enabled data source with no identity
   ends `SCAN_FAILED` with a message naming `extractionUserId`; the same scan
   with an identity granted some documents produces a graph containing exactly
   those (`documents_not_visible` equals the rest).

**Deployed run, 2026-10-31, the dev deployment (us-east-1) against KB
a test Managed KB (eu-central-1, same account), data source `acl-s3-test`
(finance=alice, hr+shared=bob, extraction identity granted all three).**
Namespace `fmkb-acl-test`, source registered through the API with
`extractionUserId`, no role (same account). Results against the items above:

| Item | Result |
|---|---|
| Pipeline | execution took `SourcesInputMode?` -> `SourcesBedrockKbSkipPreprocess` -> `SourcesKGBuild`. |
| 3 registration | `COMPLETED`, `documentsProcessed` 3, `chunksGraph` 3 = distinct `Retrieve` results as the extraction identity; `documents_not_visible` 0. |
| 6 id contract | query output carries `aws:<tenant>:kb:<_chunk_id>` ids; the budget hit's id equals `stored_chunk_id(to_graphrag_tenant_id(ns), _chunk_id)`, so the pivot is a node lookup. |
| 1, 2 probe | `Retrieve` filtered on the graph's `source_uri` (the KB's `https://` form, read off the results) with the probe query returns the entitled document; the stored value is the KB's own, so equality holds by construction. |
| 4 routing | `bedrock_kb_retriever_built` logged per namespace from the registry, no KB setting in the serve environment; the same caller got different results as the fixture ACL changed, so `userContext` was the caller. |
| 5 drift | after two data-source re-syncs (ACL-only changes) the KB's `_chunk_id`s are unchanged and a re-scan rebuilt 3 chunks; the hit from the re-synced KB resolved to the node built before the syncs. |
| Entitlement | one real user, two ACL states. Not in the ACL: three questions, 0 chunks, no document content. Granted `finance/` only: the budget question returns the finance chunk and the right answer; the HR and handbook questions return no HR or shared content. |
| 7 identity error | not run live (unit-tested); the scan with a wrong identity fails with the field named. |

Four defects found and fixed during the run, none visible to unit tests: the
in-account KB grant was scoped to the deployment's Region while the KB lived in
another (`arn:aws:bedrock:*:<account>:knowledge-base/*` now); the loader took a
listing `AccessDenied` for the SharePoint "sample instead" case (only
`ValidationException` falls back now); the extraction LLM's `max_tokens` 16384
exceeds the Amazon Nova ceiling of 10000 so every document failed at extraction
(new context knob `kg_build_extraction_max_tokens`, 8192 on dev; a pre-existing
dev issue, this was the stack's first document ingestion); and serve queried
the whole Knowledge Base rather than the registered data sources, so a
namespace received text from data sources it never registered (the KB's ACL
held, the registration's scope did not; every `Retrieve`, search and probe, is
now scoped to `dataSourceIds`). Operational: the kg-build image build under
Finch needs disk; a full local image cache failed the first deploy.

Registration, routing and pipeline unit coverage:
`packages/sources/tests/unit/api/test_document_routes.py::TestCreateBedrockManagedKbSource`
(sub-type inference, mutual exclusion, optional role shape, stored form, job
body),
`packages/sources/tests/unit/documents/trigger/test_index.py::TestBedrockKbFlattening`
(flat fields always present),
`packages/context-manager/tests/unit/test_bedrock_kb_source_routing.py`
(registry lookup, ambiguity and unavailability refused, per-source retriever
cache, same-account versus assumed session with the namespace ExternalId,
admission binding fails closed), `infra/test/services/sources-stack.test.ts`
"Fork: BEDROCK_MANAGED_KB ingestion" (routing `Choice`, bypass `Pass`, task
env bindings, `RESOURCE_PREFIX`, IAM scope, fourth assume consumer, the
published task-role parameter), `infra/test/services/serve-stack.test.ts`
(fifth assume consumer byte-equal to the sources stack's, in-account read
grant, `RESOURCE_PREFIX`, no Knowledge Base variable in the runtime
environment), `packages/web-app/src/pages/ConnectSource.test.tsx` "Bedrock
Knowledge Base (Documents tab)" (payload shape, reserved-prefix check against
the derived prefix, ExternalId never submitted, optional fields omitted) and
`SourceDetail.bedrockkb.test.tsx` (readback).
