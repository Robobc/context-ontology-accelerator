# ADR 0007: Persist namespace integration profiles

## Status

Accepted; supersedes ADR 0004's environment-only bridge as the production routing model.

## Decision

Persist a versioned integration profile with each namespace. The profile selects Accelerate, Adopt, or Federate, binds an approved ontology version, and names logical read-only source/generation targets.

Endpoints, role ARNs, tokens, and credentials are forbidden from the profile. Administrator deployment configuration maps target IDs to adapters and resource identifiers.

## Consequences

Multiple onboarding profiles can share one control plane without globally selecting one backend. Environment maps remain compatibility inputs for the stock query path, not the semantic-plan contract. Profile updates are validated and replaced atomically through the namespace API.
