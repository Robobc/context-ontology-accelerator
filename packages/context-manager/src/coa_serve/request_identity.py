# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request-scoped caller identity (fork).

The authenticated caller is resolved once per request at the entrypoint
(``resolve_profile``). Retrieval adapters several layers down need it to ask
the data store for *this user's* view (Bedrock Knowledge Base ``userContext``,
the document entitlement gate). Threading a ``user_id`` through every
``resolve()`` / ``retrieve()`` / tool signature would touch a dozen upstream
files on every rebase, so the identity is published through a ``ContextVar``
instead: set at the entrypoint, read by the adapters, automatically scoped to
the request's task tree (``asyncio`` copies the context into child tasks).

Same pattern as ``namespace_language``'s label preference.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_user_id: ContextVar[str] = ContextVar("coa_serve_request_user_id", default="")


def set_request_user(user_id: str | None) -> Token[str]:
    """Publish the authenticated caller for the current request; returns the reset token."""
    return _user_id.set((user_id or "").strip())


def get_request_user() -> str:
    """The authenticated caller of the current request, or ``""`` when none was published."""
    return _user_id.get()


def reset_request_user(token: Token[str]) -> None:
    """Restore the identity published before ``set_request_user`` (tests, nested scopes)."""
    _user_id.reset(token)
