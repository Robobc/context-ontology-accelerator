"""Trusted identity propagation from the AgentCore-validated OIDC token."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from coa_common.auth import extract_email_claim


class MissingIdentityError(ValueError):
    """Raised when AgentCore did not forward a usable bearer token."""


@dataclass(frozen=True)
class TrustedIdentity:
    """Identity extracted exclusively from the validated bearer token."""

    bearer_token: str
    subject: str
    user_id: str

    @property
    def knowledge_user_context(self) -> dict[str, str]:
        """Managed-KB ACL context; never sourced from request or model output."""
        return {"userId": self.user_id}


def from_runtime_context(context: Any) -> TrustedIdentity:
    """Derive downstream identity from a token already validated by AgentCore."""
    headers = getattr(context, "request_headers", None)
    if not isinstance(headers, dict):
        raise MissingIdentityError("Authorization header was not forwarded")
    raw = headers.get("authorization", "") or headers.get("Authorization", "")
    if not isinstance(raw, str) or not raw.startswith("Bearer "):
        raise MissingIdentityError("A Bearer token is required")
    token = raw[7:].strip()
    if not token:
        raise MissingIdentityError("A Bearer token is required")

    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.DecodeError as exc:
        raise MissingIdentityError("Bearer token is malformed") from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise MissingIdentityError("Bearer token is missing sub")
    email = extract_email_claim(claims)
    user_id = email or subject
    return TrustedIdentity(
        bearer_token=token,
        subject=subject,
        user_id=user_id,
    )
