# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for token authorizer ABC and implementations."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

pytestmark = pytest.mark.unit
from coa_common.auth import (
    CognitoTokenAuthorizer,
    OIDCTokenAuthorizer,
    TokenAuthorizer,
    TokenType,
    TokenValidationResult,
)


@pytest.fixture()
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    public_numbers = public_key.public_numbers()

    def _int_to_base64url(n: int, length: int) -> str:
        return jwt.utils.base64url_encode(n.to_bytes(length, byteorder="big")).decode()

    jwk = {
        "kty": "RSA",
        "kid": "test-kid-1",
        "use": "sig",
        "alg": "RS256",
        "n": _int_to_base64url(public_numbers.n, 256),
        "e": _int_to_base64url(public_numbers.e, 3),
    }
    return private_key, jwk


@pytest.fixture()
def _make_token(rsa_keypair):
    private_key, _ = rsa_keypair

    def _factory(claims: dict[str, Any], headers: dict[str, Any] | None = None) -> str:
        default_headers = {"kid": "test-kid-1"}
        if headers:
            default_headers.update(headers)
        return jwt.encode(claims, private_key, algorithm="RS256", headers=default_headers)

    return _factory


@pytest.fixture()
def jwks_response(rsa_keypair):
    _, jwk = rsa_keypair
    return {"keys": [jwk]}


class TestExtractBearerToken:
    def test_valid_bearer(self):
        assert TokenAuthorizer._extract_bearer_token("Bearer abc123") == "abc123"

    def test_missing_header(self):
        with pytest.raises(ValueError, match="Missing or malformed"):
            TokenAuthorizer._extract_bearer_token("")

    def test_no_bearer_prefix(self):
        with pytest.raises(ValueError, match="Missing or malformed"):
            TokenAuthorizer._extract_bearer_token("Basic abc123")

    def test_empty_token(self):
        with pytest.raises(ValueError, match="Bearer token is empty"):
            TokenAuthorizer._extract_bearer_token("Bearer ")


class TestCognitoTokenAuthorizer:
    def test_validate_success(self, _make_token, jwks_response):
        claims = {
            "sub": "user-123",
            "email": "alice@example.com",
            "cognito:groups": ["Admins", "Viewers"],
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert isinstance(result, TokenValidationResult)
        assert result.principal_id == "alice@example.com"
        assert result.sub == "user-123"
        assert result.email == "alice@example.com"
        assert result.groups == ["Admins", "Viewers"]
        assert result.token_type == TokenType.JWT

    def test_expired_token_rejected(self, _make_token, jwks_response):
        claims = {
            "sub": "user-123",
            "email": "alice@example.com",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) - 100,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_recently_expired_token_accepted_within_leeway(self, _make_token, jwks_response):
        """Token expired 10s ago is accepted due to 30s leeway."""
        claims = {
            "sub": "user-123",
            "email": "alice@example.com",
            "cognito:groups": ["Admin"],
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) - 10,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.principal_id == "alice@example.com"

    def test_token_expired_beyond_leeway_rejected(self, _make_token, jwks_response):
        """Token expired 31s ago (beyond 30s leeway) is rejected."""
        claims = {
            "sub": "user-123",
            "email": "alice@example.com",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) - 31,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_missing_email_denied(self, _make_token, jwks_response):
        claims = {
            "sub": "user-456",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="missing required email"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_no_groups_returns_empty(self, _make_token, jwks_response):
        claims = {
            "sub": "user-456",
            "email": "bob@example.com",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.groups == []


class TestOIDCTokenAuthorizer:
    def test_validate_success(self, _make_token, jwks_response):
        claims = {
            "sub": "okta-user-1",
            "email": "carol@corp.com",
            "groups": ["Engineering", "Platform"],
            "iss": "https://login.corp.com",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(issuer="https://login.corp.com", jwks_uri="https://login.corp.com/jwks")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.principal_id == "carol@corp.com"
        assert result.groups == ["Engineering", "Platform"]

    def test_custom_group_claim_name(self, _make_token, jwks_response):
        claims = {
            "sub": "u1",
            "email": "user@corp.com",
            "memberOf": ["TeamA", "TeamB"],
            "iss": "https://idp.example.com",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", group_claim_name="memberOf")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.groups == ["TeamA", "TeamB"]

    def test_entra_preferred_username_is_email_fallback(self, _make_token, jwks_response):
        claims = {
            "sub": "opaque-entra-sub",
            "preferred_username": "user@contoso.com",
            "roles": ["data-steward"],
            "iss": "https://login.microsoftonline.com/tenant-id/v2.0",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(
            issuer="https://login.microsoftonline.com/tenant-id/v2.0",
            group_claim_name="roles",
        )

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.email == "user@contoso.com"
        assert result.principal_id == "user@contoso.com"
        assert result.groups == ["data-steward"]

    def test_groups_as_csv_string(self, _make_token, jwks_response):
        claims = {
            "sub": "u1",
            "email": "user@corp.com",
            "groups": "A,B,C",
            "iss": "https://idp.example.com",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.groups == ["A", "B", "C"]

    def test_issuer_mismatch_rejected(self, _make_token, jwks_response):
        claims = {
            "sub": "u1",
            "email": "user@evil.com",
            "iss": "https://evil.com",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(issuer="https://legit.com")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            authorizer.validate(f"Bearer {token}")


class TestTokenType:
    def test_token_type_is_enum(self):
        assert TokenType.JWT == "JWT"
        assert TokenType.AGENT_ACCESS_TOKEN == "AGENT_ACCESS_TOKEN"


class TestVerifyClaimsAndExtractGroups:
    """verify_claims/extract_groups: the email-agnostic path used by callers
    (e.g. MCP server) whose identity model isn't email-keyed."""

    def test_verify_claims_returns_raw_claims_without_email_requirement(self, _make_token, jwks_response):
        claims = {
            "sub": "agent-user-1",
            "cognito:groups": ["admin"],
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.verify_claims(f"Bearer {token}")

        assert result["sub"] == "agent-user-1"

    def test_verify_claims_still_rejects_forged_signature(self, jwks_response):
        forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(
            {
                "sub": "attacker",
                "cognito:groups": ["admin"],
                "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
                "exp": int(time.time()) + 3600,
            },
            forged_key,
            algorithm="RS256",
            headers={"kid": "test-kid-1"},
        )
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            authorizer.verify_claims(f"Bearer {forged}")

    def test_extract_groups_delegates_to_idp_specific_hook(self):
        cognito = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")
        assert cognito.extract_groups({"cognito:groups": ["a", "b"]}) == ["a", "b"]

        oidc = OIDCTokenAuthorizer(issuer="https://idp.example.com", group_claim_name="memberOf")
        assert oidc.extract_groups({"memberOf": ["c", "d"]}) == ["c", "d"]


class TestJWKSCaching:
    def test_jwks_cached_within_ttl(self, jwks_response):
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri="https://idp.example.com/jwks")
        with patch("coa_common.auth.token_authorizer.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = jwks_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            authorizer._get_jwks()
            authorizer._get_jwks()
            assert mock_get.call_count == 1

    def test_jwks_refetched_after_ttl(self, jwks_response):
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri="https://idp.example.com/jwks")
        authorizer._jwks_cache_ttl = 0

        with patch("coa_common.auth.token_authorizer.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = jwks_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            authorizer._get_jwks()
            authorizer._get_jwks()
            assert mock_get.call_count == 2
