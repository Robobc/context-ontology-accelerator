"""Fork (ADR 0011): graph node ids for Bedrock Knowledge Base chunks and documents.

The lexical graph is keyed on the Knowledge Base's own ids so that a ``Retrieve``
hit at query time *is* a graph node, with no translation table. Three services
share that contract and must agree byte for byte: the kg-build loader emits the
ids, the serve retriever and entitlement gate parse and reconstruct them, and
the ontology engine recognises them to refuse induction. This module is the
single place the shape is written down.

The shape is dictated by graphrag-toolkit, which rewrites every node id for the
tenant at build time (``NodeBuilders.pre_process`` -> ``TenantId.rewrite_id``):

    parts = id.split(":")            # the toolkit's own ids are aws::<h8>:<h4>[...]
    f"{parts[0]}:{tenant}:{':'.join(parts[2:])}"

Segment 1 is REPLACED by the tenant and segments 0 and 2+ are kept, so an id
emitted as ``aws::<tail>`` is stored as ``aws:<tenant>:<tail>`` and nothing is
lost. An id emitted as ``aws:<tail>`` (two segments) is stored as
``aws:<tenant>:`` -- the tail is dropped and every chunk collapses onto one node.
The extract stage's ``IdRewriter`` leaves any ``aws:`` id alone, which is the
half of the contract the first implementation verified; this module encodes
the other half. ``test_kb_ids.py`` runs the toolkit's real ``rewrite_id`` on
the emitted ids so the two cannot drift apart again.

Emitted (by kg-build, before the toolkit sees them):

    aws::kb:<_chunk_id>          __Chunk__
    aws::doc:<document-id-tail>  __Source__

Stored (what serve and the ontology engine read):

    aws:<tenant>:kb:<_chunk_id>
    aws:<tenant>:doc:<document-id-tail>

The markers ``kb`` and ``doc`` at segment 2 are what distinguish Knowledge Base
content from the toolkit's own nodes (``aws:<tenant>:<hex8>:<hex4>...``), which
are not under any document ACL and pass the gate untouched. The ``_chunk_id``
is base64url and fits the toolkit's id grammar as-is; a Bedrock ``documentId``
may not (for an S3 data source it is the ``s3://`` URI), so the document tail
is the id with ``#`` -> ``_`` when that is clean, else a hash of it. The raw
ids are kept on the nodes as ``bedrockChunkId`` / ``bedrockDocumentId``, and the
gate resolves a source to its ``_source_uri`` through a property, never by
parsing the id.
"""

from __future__ import annotations

import hashlib
import re

ID_ROOT = "aws"
KB_CHUNK_MARKER = "kb"
KB_DOC_MARKER = "doc"

_ID_SEGMENT_OK = re.compile(r"^[A-Za-z0-9._-]+$")
_HASHED_DOC_PREFIX = "h."


def emit_chunk_id(bedrock_chunk_id: str) -> str:
    """``aws::kb:<_chunk_id>``, the id kg-build gives a ``__Chunk__`` node."""
    if not bedrock_chunk_id or not _ID_SEGMENT_OK.match(bedrock_chunk_id):
        raise ValueError("Bedrock _chunk_id is empty or carries characters outside the id grammar")
    return f"{ID_ROOT}::{KB_CHUNK_MARKER}:{bedrock_chunk_id}"


def document_id_tail(bedrock_document_id: str) -> str:
    """The id-grammar-safe tail for a Bedrock ``documentId``.

    ``#`` -> ``_`` keeps the SharePoint shape (``4#<uuid>#<sha256>``) readable;
    anything else outside the grammar (an ``s3://`` URI, a custom id with
    spaces) becomes ``h.<sha256[:32]>``. Deterministic, collision-free in
    practice, and never parsed back: the raw id lives on the node.
    """
    if not bedrock_document_id:
        raise ValueError("Bedrock documentId is empty")
    readable = bedrock_document_id.replace("#", "_")
    if _ID_SEGMENT_OK.match(readable):
        return readable
    return _HASHED_DOC_PREFIX + hashlib.sha256(bedrock_document_id.encode("utf-8")).hexdigest()[:32]


def emit_document_id(bedrock_document_id: str) -> str:
    """``aws::doc:<tail>``, the id kg-build gives a ``__Source__`` node."""
    return f"{ID_ROOT}::{KB_DOC_MARKER}:{document_id_tail(bedrock_document_id)}"


def stored_chunk_id(tenant_id: str, bedrock_chunk_id: str) -> str:
    """The ``__Chunk__`` node id as it exists in the graph of ``tenant_id``.

    What the serve retriever hands out for a ``Retrieve`` hit so the hit pivots
    straight into graph traversal. ``tenant_id`` is ``to_graphrag_tenant_id(namespace)``;
    the empty string is the toolkit's default tenant, whose rewrite is the identity.
    """
    return f"{ID_ROOT}:{tenant_id}:{KB_CHUNK_MARKER}:{bedrock_chunk_id}"


def _marker_of(node_id: str) -> str | None:
    parts = node_id.split(":")
    if len(parts) < 4 or parts[0] != ID_ROOT:
        return None
    return parts[2]


def is_kb_chunk_id(node_id: str) -> bool:
    """True for a ``__Chunk__`` id minted from a Knowledge Base chunk, in any tenant."""
    return _marker_of(node_id) == KB_CHUNK_MARKER


def is_kb_source_id(node_id: str) -> bool:
    """True for a ``__Source__`` id minted from a Knowledge Base document, in any tenant."""
    return _marker_of(node_id) == KB_DOC_MARKER


def bedrock_chunk_id_of(node_id: str) -> str | None:
    """Inverse of :func:`stored_chunk_id`: the ``_chunk_id``, or ``None`` for a non-KB id."""
    if not is_kb_chunk_id(node_id):
        return None
    return node_id.split(":", 3)[3]


# openCypher fragment for "this __Source__ node is a Knowledge Base document", for
# stores that test the id in the query rather than in Python. ``:doc:`` cannot
# occur in a toolkit-minted id (those segments are hex) nor in a tenant (hex).
KB_SOURCE_ID_CYPHER_PREDICATE = f"id(s) STARTS WITH '{ID_ROOT}:' AND id(s) CONTAINS ':{KB_DOC_MARKER}:'"
