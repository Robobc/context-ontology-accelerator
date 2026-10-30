# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request-scoped document source binding (fork).

Which Bedrock Managed Knowledge Base a namespace reads its documents from is a
fact of that namespace's source registry (its ``BEDROCK_MANAGED_KB`` source),
exactly as the Databricks or Glue source a Tier-2 query routes to is. The
admission gate resolves it once per request, right where it publishes the
caller identity, and the document retriever and the entitlement gate several
layers down read the binding instead of a deployment-level map.

Same ``ContextVar`` pattern as :mod:`coa_serve.request_identity`, for the same
reason: threading the source through every upstream ``retrieve()`` / tool
signature would touch a dozen upstream files on every rebase. The binding is
cleared on entry to the entrypoint and restored on exit, so a worker that served
a Knowledge Base namespace never carries that binding into its next request.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BoundDocumentSource:
    """The per-user Knowledge Base retriever admitted for one request's namespace."""

    namespace: str
    retriever: Any  # BedrockKnowledgeBaseRetriever with enforce_user_context=True


_bound: ContextVar[BoundDocumentSource | None] = ContextVar("coa_serve_request_document_source", default=None)


def bind_request_document_source(bound: BoundDocumentSource | None) -> Token[BoundDocumentSource | None]:
    """Publish the request's Knowledge Base source (``None`` = the namespace has none); returns the reset token."""
    return _bound.set(bound)


def get_request_document_source() -> BoundDocumentSource | None:
    """The Knowledge Base source bound to the current request, or ``None``."""
    return _bound.get()


def reset_request_document_source(token: Token[BoundDocumentSource | None]) -> None:
    """Restore the binding that preceded ``bind_request_document_source`` (entrypoint exit, tests)."""
    _bound.reset(token)
