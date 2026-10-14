# ADR 0003: Use Bedrock Knowledge Bases for document retrieval only

- Status: Accepted for the integration PoC
- Date: 2026-09-21

## Decision

Add a document-retriever protocol and a Bedrock Knowledge Base implementation.
A global default and explicit namespace-to-KB routes can select Managed KB
retrieval without changing the REST or MCP search contract.

Retain COA's existing OpenSearch collection for ontology entities and their
embeddings. Managed KB supplies ranked document chunks only; it does not become
COA's ontology/entity vector store.

## Consequences

- A managed-document namespace can reuse its existing corpus without re-ingesting it into a
  second COA document index.
- COA ontology search, entity linking, and schema responsibilities remain
  separate from Managed KB.
- The execution role receives `bedrock:Retrieve` only for explicitly configured
  knowledge-base ARNs.
- Managed KB lexical baselines are unavailable through the Retrieve API, so
  evaluation must compare retrieval quality rather than assume hybrid-search
  parity.
- The PoC route may target only a KB whose entire corpus is authorized for that
  namespace. Per-document ACL-to-retrieval-filter translation is not yet wired
  and remains required before a mixed-entitlement production corpus is safe.
