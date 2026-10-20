# COA MCP Proxy

Stdio bridge for connecting AI coding assistants (Kiro, Claude Desktop, Cursor) to the COA AgentCore MCP Runtime.

Handles Cognito PKCE authentication automatically: browser login on first run, silent token refresh after that (30-day sessions).

## How it works

```
IDE (stdio) → mcp-proxy (Streamable HTTP + Bearer JWT) → AgentCore MCP Runtime
```

1. Auto-discovers runtime config from SSM (`/coa/mcp/runtime-arn`, `/coa/mcp-client-id`, `/coa/issuer`)
2. Authenticates via Cognito PKCE (opens browser, no stored passwords)
3. Caches refresh token in `~/.scl/tokens.json` (600 permissions)
4. Launches `mcp-proxy` to bridge stdio to Streamable HTTP with the JWT

## Setup

### Prerequisites

- Node.js 18+
- [uv](https://docs.astral.sh/uv/) (for `uvx mcp-proxy`)
- AWS credentials that can read SSM parameters (any readonly role)

### Install

From the repo root:

```bash
pnpm install
```

### Build

```bash
cd packages/mcp-proxy
npx tsc --outDir dist
```

### Configure Kiro

Add to `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "coa-dev": {
      "command": "node",
      "args": ["packages/mcp-proxy/dist/index.js"],
      "env": {
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "your-profile"
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

If Kiro reports `spawn node ENOENT`, replace `"node"` with the full path from `which node`.

### Environment Variables

All optional (auto-discovered from SSM if not set):

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region | `us-east-1` |
| `AWS_PROFILE` | AWS profile for SSM lookup | default credential chain |
| `SCL_PREFIX` | SSM parameter prefix | `/scl` |
| `SCL_CLIENT_ID` | Override Cognito client ID | from SSM |
| `SCL_ISSUER` | Override Cognito issuer URL | from SSM |
| `SCL_RUNTIME_ARN` | Override runtime ARN | from SSM |
| `SCL_RUNTIME_URL` | Override full invocations URL | built from ARN |

### First Run

On first connection, the proxy opens your browser to the Cognito login page. Sign in with your credentials. The token is cached for 30 days. No password is stored anywhere on disk.

### Token Refresh

The cached refresh token in `~/.scl/tokens.json` automatically renews the session. If it expires (after 30 days of inactivity), the browser login will open again.

To force re-login:

```bash
rm ~/.scl/tokens.json
```
