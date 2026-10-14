# Microsoft Entra ID for COA

COA uses Entra directly as a public OIDC client. Cognito federation is not
required.

The registration has two platform-specific localhost callbacks:

- SPA: `http://localhost:5173/authenticate/`
- Public client (MCP/CLI): `http://localhost:9876/oauth/callback`

Create the registration with:

```bash
az ad app create \
  --display-name "COA Enterprise Integration PoC" \
  --sign-in-audience AzureADMyOrg \
  --is-fallback-public-client true \
  --public-client-redirect-uris http://localhost:9876/oauth/callback \
  --app-roles @infra/entra/app-roles.json \
  --optional-claims @infra/entra/optional-claims.json \
  --requested-access-token-version 2
```

The high-level Azure CLI updater cannot initialize an empty SPA platform. Set
`spa.redirectUris` on the application object through `az rest` using
`spa-redirects.localhost.json`.

Configure COA with the tenant-specific v2 issuer, the application/client ID,
and `roles` as the group claim:

```text
issuer:     https://login.microsoftonline.com/<tenant-id>/v2.0
jwksUri:    https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys
clientId:   <application-client-id>
groupClaim: roles
scopes:     openid email profile
```

An Entra app role only places a value in the token's `roles` claim. It does not
grant a COA permission by itself. Create the matching COA Group grant for each
namespace (or a platform grant for `platform-admin`) after deployment.

Replace the localhost SPA callback with the deployed CloudFront callback before
using this registration outside local development.
