# ADR 0004: Treat environment routing maps as a PoC bridge

- Status: Superseded by ADR 0007 for semantic-plan routing; retained for stock query compatibility
- Date: 2026-09-21

## Decision

Use environment configuration to route named namespaces to Managed KBs during
the integration PoC. Keep the document-retriever protocol namespace-aware so this
configuration can later move behind a persisted namespace integration profile.

Do not represent the environment map as the final hub-and-spoke control plane.
The production target is a namespace record that selects adapters for document
retrieval, graph/ABox access, and model generation while preserving the same
REST, MCP, and authorization contracts.

## Consequences

- The PoC can exercise managed-KB and default-search namespaces through the stock
  query path.
- Semantic-plan routing now uses the persisted namespace profile defined by ADR
  0007; changing that profile does not require a Serve redeployment.
- The global generation/document maps remain compatibility inputs only and are
  not presented as the multi-profile platform contract.
