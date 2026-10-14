# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""JWT claims extraction with independent signature verification.

AgentCore Runtime's ``RuntimeAuthorizerConfiguration.usingJWT`` already
verifies signature/issuer/audience against the configured IdP's JWKS before
a request reaches this server (see infra/lib/stacks/services/mcp-stack.ts).
This module adds defense-in-depth on top of that platform control: if the
AgentCore authorizer is ever misconfigured, disabled, or bypassed by a caller
reaching this code through a path that skips it, an attacker-forged JWT
(e.g. self-signed with ``{"roles": ["admin"]}``) must still fail here rather
than being trusted blindly.

Reuses ``coa_common.auth.build_token_authorizer`` — the same IdP-agnostic
JWKS/RS256 verification already relied on by the control-plane API Gateway
authorizer — rather than re-implementing JWKS fetch/cache/signature-check
logic here. Works with any OIDC-compliant IdP (Cognito, Okta, EntraID,
Auth0, ...), selected by ``JWT_ISSUER_URL`` at deploy time; no MCP-specific
IdP assumption is hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import structlog
from coa_common.auth import TokenAuthorizer, build_token_authorizer, extract_email_claim

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CallerIdentity:
    """Extracted identity from the JWT claims."""

    user_id: str
    agent_id: str | None
    namespaces: list[str]
    roles: list[str]
    raw_claims: dict
    email: str = ""


class ClaimsExtractionError(Exception):
    """Raised when the token is missing, invalid, or lacks required claims."""


@lru_cache(maxsize=1)
def _get_authorizer() -> TokenAuthorizer:
    """Build the token authorizer once per container (JWKS is cached internally).

    ``JWT_ISSUER_URL`` / ``JWT_CLIENT_ID`` are set by mcp-stack.ts from the same
    IdP coordinates AgentCore's ``RuntimeAuthorizerConfiguration.usingJWT``
    uses, so this independent check validates against the same trust anchor.
    Cleared via ``_get_authorizer.cache_clear()`` in tests. Missing
    configuration raises on first call — fail closed, never silently skip
    verification.
    """
    issuer = os.environ["JWT_ISSUER_URL"]
    return build_token_authorizer(
        issuer=issuer,
        region=os.environ["AWS_REGION"],
        client_id=os.environ.get("JWT_CLIENT_ID"),
        jwks_uri=os.environ.get("JWT_JWKS_URI") or None,
        group_claim_name=os.environ.get("GROUP_CLAIM_NAME", "groups"),
    )


def extract_caller_identity(authorization_header: str | None) -> CallerIdentity:
    """Verify the JWT's signature and extract the delegating user's identity.

    Args:
        authorization_header: The full Authorization header value (e.g. "Bearer <token>").

    Returns:
        CallerIdentity with the delegating user's info.

    Raises:
        ClaimsExtractionError: If the header is missing, malformed, the
            signature/issuer/audience/expiry check fails, or required claims
            are absent.
    """
    if not authorization_header:
        raise ClaimsExtractionError("Missing Authorization header")

    authorizer = _get_authorizer()
    try:
        # verify_claims does the crypto verification (signature/issuer/audience/
        # expiry) but skips validate()'s email requirement — MCP callers are
        # keyed by "sub"/"username", never carry email.
        claims = authorizer.verify_claims(authorization_header)
    except ValueError as e:
        logger.warning("jwt_verification_failed", error=str(e))
        raise ClaimsExtractionError(f"JWT verification failed: {e}") from e

    user_id = claims.get("sub") or claims.get("user_id") or claims.get("username")
    if not user_id:
        raise ClaimsExtractionError("JWT lacks required 'sub' / 'user_id' claim for delegating user")

    # Agent identity — logged for audit but NOT used for authorization
    agent_id = claims.get("agent_id") or claims.get("client_id") or claims.get("azp")

    # Namespace access (from custom claims or scope)
    namespaces = _extract_list_claim(claims, "namespaces", "custom:namespaces", "scl_namespaces")

    # Roles — reuses the same group-claim extraction TokenAuthorizer subclasses
    # use (cognito:groups for Cognito, configurable claim name for generic OIDC),
    # so the IdP-specific claim-name handling lives in exactly one place.
    roles = authorizer.extract_groups(claims)

    logger.info(
        "caller_identity_extracted",
        user_id=user_id,
        agent_id=agent_id,
        namespace_count=len(namespaces),
    )

    return CallerIdentity(
        user_id=user_id,
        agent_id=agent_id,
        namespaces=namespaces,
        roles=roles,
        raw_claims=claims,
        email=extract_email_claim(claims),
    )


def _extract_list_claim(claims: dict, *keys: str) -> list[str]:
    """Try multiple claim keys, return the first non-empty list found."""
    for key in keys:
        value = claims.get(key)
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str) and value:
            return [v.strip() for v in value.split(",") if v.strip()]
    return []
