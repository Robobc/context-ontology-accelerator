# MCP Server

- **Owner**: avijitg
- **LLD**: Serve LLD §3.2.5 (Story 5)
- **Work Item**: #128 (tool definitions + auth), #129 (AgentCore Runtime deployment)
- **Status**: In Progress

## Overview

Exposes Context Ontology Accelerator capabilities as 7 discoverable MCP tools for AI agents via Streamable HTTP
on AgentCore Runtime (`--protocol MCP`, port 8000).

**Architecture**: Thin protocol adapter on a separate AgentCore Runtime.
- Discovery tools (list_metrics, describe_schema) invoke backend Lambdas directly via `lambda:InvokeFunction`
- Execution tools (query, translate_sparql, rag_retrieval, graph_traversal) delegate to the Context Manager via its AgentCore HTTP invocations endpoint
- MCP server handles: MCP protocol framing, JWT extraction, pre-auth (Cedar), audit logging
- Context Manager handles: orchestration, data access (Neptune, Athena, OpenSearch, Bedrock)

The MCP server does NOT run the orchestrator in-process. It has minimal direct data access.
Needs DynamoDB (role resolution), Lambda invoke (discovery), and HTTPS egress (to invoke the CM).

## Tools

| Tool | Category | Description |
|------|----------|-------------|
| `list_metrics` | Discovery | List governed metric definitions (name, description, dimensions) |
| `describe_schema` | Discovery | Describe ontology classes, properties, and data source tables |
| `query` | Execution | End-to-end NL query with tiered resolution |
| `semantic_execution_plan` | Planning | Return a versioned, non-executing plan for source-native tools |
| `translate_sparql` | Execution | Translate NL to SPARQL using the published ontology |
| `translate_sql` | Execution | Translate NL to governed SQL without executing it |
| `rag_retrieval` | Execution | Retrieve semantically similar document chunks |
| `graph_traversal` | Execution | Traverse the semantic graph for entity relationships |

## Running Locally

```bash
# Full mode (needs deployed metric-service + ontology-engine Lambdas + Context Manager)
SCL_AUTH_ENABLED=false \
CM_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/your_cm_runtime \
METRIC_SERVICE_LAMBDA_ARN=arn:aws:lambda:us-east-1:123456789012:function:coa-dev-metric-api \
ONTOLOGY_PROXY_LAMBDA_ARN=arn:aws:lambda:us-east-1:123456789012:function:coa-dev-ontology-api-proxy \
  uv run python -m coa_mcp

# Test with MCP Inspector
npx @modelcontextprotocol/inspector uv run python -m coa_mcp
```

## IDE Integration (Kiro / Claude Desktop)

Connect your IDE to the deployed MCP server using `mcp-proxy` as a stdio-to-streamable-http bridge.

### Prerequisites

- `uvx` installed (`pip install uv`)
- AWS credentials with access to SSM + Secrets Manager + Cognito
- Deployed `coa-dev-mcp` stack
- `jq` installed

### How it works

```
IDE (stdio) --> wrapper script --> mcp-proxy --> HTTPS (Bearer JWT) --> AgentCore Runtime --> MCP Server
```

You need two shell scripts: one to fetch a Cognito token, and one to launch the proxy.

### Step 1: Create `get-token.sh`

This script fetches a Cognito ID token. It accepts credentials via `SCL_USERNAME`/`SCL_PASSWORD`/`SCL_CLIENT_ID` env vars, or falls back to Secrets Manager.

```bash
#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_PROFILE="${AWS_PROFILE:-}"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-coa}"
ENV_NAME="${ENV_NAME:-dev}"
SECRET_NAME="${RESOURCE_PREFIX}-${ENV_NAME}-integ-test-user"

PROFILE_FLAG=""
if [[ -n "$AWS_PROFILE" ]]; then
  PROFILE_FLAG="--profile $AWS_PROFILE"
fi

# Direct credentials (preferred for IDE usage)
if [[ -n "${SCL_USERNAME:-}" && -n "${SCL_PASSWORD:-}" && -n "${SCL_CLIENT_ID:-}" ]]; then
  USERNAME="$SCL_USERNAME"
  PASSWORD="$SCL_PASSWORD"
  CLIENT_ID="$SCL_CLIENT_ID"
else
  # Fall back to Secrets Manager
  SECRET_JSON=$(aws secretsmanager get-secret-value \
    $PROFILE_FLAG \
    --secret-id "$SECRET_NAME" \
    --region "$AWS_REGION" \
    --query SecretString \
    --output text)
  USERNAME=$(echo "$SECRET_JSON" | jq -r '.username')
  PASSWORD=$(echo "$SECRET_JSON" | jq -r '.password')
  CLIENT_ID=$(echo "$SECRET_JSON" | jq -r '.userPoolClientId')
fi

# Get Cognito ID token
TOKEN=$(aws cognito-idp initiate-auth \
  $PROFILE_FLAG \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id "$CLIENT_ID" \
  --auth-parameters "USERNAME=$USERNAME,PASSWORD=$PASSWORD" \
  --region "$AWS_REGION" \
  --query 'AuthenticationResult.IdToken' \
  --output text)

echo "$TOKEN"
```

### Step 2: Create `start-mcp-proxy.sh`

This script resolves the runtime ARN from SSM, fetches a token, and launches `mcp-proxy`.

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_PROFILE="${AWS_PROFILE:-}"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-scl}"
ENV_NAME="${ENV_NAME:-dev}"
SSM_KEY="/${RESOURCE_PREFIX}/mcp/runtime-arn"

PROFILE_FLAG=""
if [[ -n "$AWS_PROFILE" ]]; then
  PROFILE_FLAG="--profile $AWS_PROFILE"
fi

# Resolve runtime ARN
if [[ -z "${MCP_RUNTIME_ARN:-}" ]]; then
  MCP_RUNTIME_ARN=$(aws ssm get-parameter \
    $PROFILE_FLAG \
    --name "$SSM_KEY" \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text)
fi

# URL-encode the ARN
ENCODED_ARN=$(python3 -c "from urllib.parse import quote; print(quote('$MCP_RUNTIME_ARN', safe=''))")
MCP_URL="https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT"

# Get fresh Cognito token
TOKEN=$("$SCRIPT_DIR/get-token.sh")

# Launch mcp-proxy in stdio -> streamable-http mode
exec uvx mcp-proxy \
  --transport streamablehttp \
  --headers Authorization "Bearer $TOKEN" \
  "$MCP_URL"
```

Make both executable: `chmod +x get-token.sh start-mcp-proxy.sh`

### Step 3: Configure your IDE

Add to `.kiro/settings/mcp.json` (or Claude Desktop's `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "coa-dev": {
      "command": "/path/to/start-mcp-proxy.sh",
      "args": [],
      "env": {
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "your-admin-profile",
        "RESOURCE_PREFIX": "coa",
        "ENV_NAME": "dev",
        "SCL_USERNAME": "you@amazon.com",
        "SCL_CLIENT_ID": "your-cognito-client-id",
        "SCL_PASSWORD": "your-cognito-password"
      },
      "autoApprove": [
        "list_metrics",
        "describe_schema",
        "query",
        "translate_sparql",
        "translate_sql",
        "rag_retrieval",
        "graph_traversal"
      ]
    }
  }
}
```

To find your `SCL_CLIENT_ID`:
```bash
aws ssm get-parameter --name "/coa/userpool-client-id" --region us-east-1 --query 'Parameter.Value' --output text
```

### Token expiry

Cognito ID tokens last 1 hour. If your session runs longer, reconnect the MCP server from the IDE's MCP panel (Kiro: Cmd+Shift+P, "Reconnect MCP Servers").

## Auth Model

- **Inbound JWT**: Validated by AgentCore Runtime platform (not re-validated by this server)
- **Authorization**: Cedar policies evaluated against the **delegating user** (not the agent)
- **Agent identity**: Logged for audit, never used for authorization decisions
- **Profile resolution**: User's grant resolved to `InvokeRequest.profile` before execution
- **Fail closed**: Empty/missing grant = DENY (not unrestricted access)

## Cedar Policy Configuration

Cedar evaluation is wired into the grant resolver. When `ROLES_TABLE_NAME` is set,
the server loads Cedar policies from DynamoDB and evaluates them before tool execution.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ROLES_TABLE_NAME` | Yes (prod) | DynamoDB table containing Cedar policies per role. When empty, Cedar evaluation is skipped (local dev). |
| `RRM_TABLE_NAME` | Yes (prod) | ResourceRoleMappings table for role resolution. |
| `AWS_REGION` | Yes | AWS region for DynamoDB access. |

### Tool to Cedar Action Mapping

| MCP Tool | Cedar Action | Required Role |
|----------|-------------|---------------|
| `list_metrics` | `viewNamespace` | data-analyst, data-steward, namespace-owner |
| `describe_schema` | `viewNamespace` | data-analyst, data-steward, namespace-owner |
| `query` | `query` | data-analyst, data-steward, namespace-owner |
| `translate_sparql` | `query` | data-analyst, data-steward, namespace-owner |
| `translate_sql` | `query` | data-analyst, data-steward, namespace-owner |
| `rag_retrieval` | `searchDocuments` | data-analyst, data-steward, namespace-owner |
| `graph_traversal` | `viewNamespace` | data-analyst, data-steward, namespace-owner |

### Backward Compatibility

- Cedar evaluation is **optional**. When `ROLES_TABLE_NAME` is empty (local dev, tests without DDB), Cedar evaluation is skipped entirely.
- The Playground/SSE path (context-manager) does not pass `cedar_action`, so Cedar evaluation does not apply to it.

For Cedar policy syntax and schema, see `libs/common/src/coa_authorization/seed/`.

## Integration Tests

Integration tests validate the MCP server against a live deployed AgentCore Runtime.

### Prerequisites

- Deployed `coa-dev-mcp` stack (CDK)
- Valid Cognito user tokens
- Network access to the AgentCore endpoint

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_RUNTIME_ARN` | Yes | Deployed MCP AgentCore Runtime ARN (from `coa-dev-mcp` stack outputs) |
| `MCP_BEARER_TOKEN` | Yes | Valid Cognito JWT for a user WITH namespace access |
| `MCP_NO_ACCESS_TOKEN` | No | Valid Cognito JWT for a user WITHOUT namespace grants (for authz denial tests; skipped if unset) |
| `MCP_TEST_NAMESPACE` | Yes | Namespace ID the bearer token user has access to |
| `AWS_REGION` | No | Defaults to `us-east-1` |

### Running

```bash
export MCP_RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/..."
export MCP_BEARER_TOKEN="eyJ..."
export MCP_TEST_NAMESPACE="550e8400-..."
export MCP_NO_ACCESS_TOKEN="eyJ..."  # optional

uv run pytest packages/mcp-server/tests/integ -m integ -v --tb=short
```
