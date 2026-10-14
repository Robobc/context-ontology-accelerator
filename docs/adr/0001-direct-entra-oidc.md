# ADR 0001: Use Microsoft Entra ID through COA's direct OIDC mode

- Status: Accepted for the integration PoC
- Date: 2026-09-21

## Decision

Use COA's existing generic OIDC path with a tenant-specific Entra v2 issuer.
Configure `roles` as the group claim and use Authorization Code with PKCE for
the browser and MCP public clients.

Request the optional `email` claim. When Entra does not emit it, normalize
`preferred_username` and then `upn` as the email-like COA principal. Apply this
normalization consistently in the control plane, Context Manager, and MCP
server.

## Consequences

- No Cognito user pool or Entra-to-Cognito federation is introduced.
- App-role values are identity attributes, not authorization grants. Cedar/RRM
  remains authoritative.
- COA does not resolve Entra group-overage references. The PoC therefore uses
  app roles, whose small token footprint is deterministic.
- The PoC registration has localhost callbacks. A deployed callback must be
  added explicitly for each environment.
