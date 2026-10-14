# ADR 0002: Route generation through Portkey without moving embeddings or guardrails

- Status: Accepted for the integration PoC
- Date: 2026-09-21

## Decision

Add a Portkey implementation behind COA's existing LLM client contract.
Portkey receives OpenAI-compatible chat-completion requests for generation.
COA embeddings continue to use Bedrock directly.

Apply Amazon Bedrock Guardrails independently before sending user content to
Portkey and after receiving model output. Buffer a streamed response until the
output guardrail passes so unsafe tokens are never released and then recalled.

Credentials are read from Secrets Manager in deployed environments. The base
URL, Portkey model identifier, secret ARN, virtual key, and config identifier
remain configuration.

## Consequences

- REST, MCP, and application call sites do not change.
- Guardrail enforcement does not depend on Portkey forwarding Bedrock Converse
  guardrail configuration.
- Generation is not streamed token-by-token in Portkey mode during the PoC.
- The live Portkey endpoint/header contract must be tested against the target deployment's
  chosen Portkey deployment before production acceptance.
