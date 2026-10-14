# ADR 0005: Separate logical chat sessions from AgentCore transport sessions

- Status: Accepted for the enterprise integration fork
- Date: 2026-09-22

## Context

The web application used the authenticated OIDC `sub` as the basis for
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`. The value was deterministic, so
all requests from one browser identity reused one AgentCore transport session.
AgentCore sticky routing then kept that user on the runtime worker/version that
accepted the first request, including after a new version became live on the
`DEFAULT` endpoint.

COA already has a separate logical conversation session. Its identifier is sent
in the request payload and its state is held in AgentCore Memory and DynamoDB.
It does not depend on process-local state in one runtime worker.

## Decision

Create a fresh UUID-based AgentCore transport session ID for every invocation.
Keep the logical COA session ID unchanged in the request payload.

The transport ID:

- is prefixed with `rt-` for log recognition;
- is at least 33 characters, satisfying AgentCore's service constraint;
- contains no OIDC subject or other principal value; and
- is never reused to express conversation continuity.

## Consequences

- Each new request is eligible for routing to the live runtime version.
- Deployments do not require a browser reload or a separately propagated
  deployment generation to escape an old worker.
- Logical chat history, trace lookup and multi-turn behavior remain unchanged.
- Requests from the same chat may execute on different workers. This is
  intentional and verifies that Context Manager remains horizontally scalable
  and does not depend on worker-local conversation state.
- Worker affinity cannot be used as a performance cache. Shared or durable
  caches must be explicit platform components.

## Rejected alternatives

### Continue deriving the transport ID from OIDC `sub`

This preserves the defect: a stable user identity becomes stable runtime
version affinity.

### Add a deployment generation to `runtime-config.json`

This only rotates after the browser reloads or polls the file, and every
partial Serve deployment would also need to update the Web stack. A
per-invocation ID solves the routing problem without cross-stack deployment
coordination.

### Rotate on a timer

This leaves a bounded but unnecessary interval in which requests continue
reaching the old version and can change transport affinity midway through an
unrelated time window.
