# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Knowledge Base id contract shared by kg-build, serve and the ontology engine (ADR 0011).

The toolkit-side half (that ``TenantId.rewrite_id`` maps the emitted ids onto the
stored ids losslessly) is pinned where the toolkit is installed:
``packages/sources/tests/unit/documents/kg_build/test_bedrock_kb_loader.py::TestToolkitIdContract``.
This file pins the pure functions and the model of the rewrite they rely on.
"""

import pytest
from coa_common import kb_ids

TENANT = "550e8400e29b41d4a71644665"
CHUNK = "Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw"


def toolkit_rewrite(node_id: str, tenant: str) -> str:
    """``TenantId.rewrite_id`` as graphrag-lexical-graph 3.18.7 implements it."""
    parts = node_id.split(":")
    return f"{parts[0]}:{tenant}:{':'.join(parts[2:])}"


@pytest.mark.unit
class TestEmittedShape:
    def test_chunk_id_has_an_empty_tenant_slot(self):
        assert kb_ids.emit_chunk_id(CHUNK) == f"aws::kb:{CHUNK}"

    def test_document_tail_keeps_sharepoint_ids_and_hashes_uris(self):
        assert kb_ids.emit_document_id("4#u#s") == "aws::doc:4_u_s"
        hashed = kb_ids.emit_document_id("s3://bucket/path/to/file.txt")
        assert hashed.startswith("aws::doc:h.") and len(hashed) == len("aws::doc:h.") + 32
        assert hashed != kb_ids.emit_document_id("s3://bucket/path/to/other.txt")

    @pytest.mark.parametrize("bad", ["", "a b", "a/b", "a:b", "q?"])
    def test_chunk_ids_outside_the_grammar_are_refused(self, bad):
        with pytest.raises(ValueError):
            kb_ids.emit_chunk_id(bad)

    def test_empty_document_id_is_refused(self):
        with pytest.raises(ValueError):
            kb_ids.emit_document_id("")


@pytest.mark.unit
class TestStoredShape:
    def test_rewrite_is_lossless_and_matches_stored_chunk_id(self):
        stored = toolkit_rewrite(kb_ids.emit_chunk_id(CHUNK), TENANT)
        assert stored == kb_ids.stored_chunk_id(TENANT, CHUNK) == f"aws:{TENANT}:kb:{CHUNK}"
        assert kb_ids.bedrock_chunk_id_of(stored) == CHUNK

    def test_default_tenant_is_the_identity(self):
        emitted = kb_ids.emit_chunk_id(CHUNK)
        assert kb_ids.stored_chunk_id("", CHUNK) == emitted and kb_ids.is_kb_chunk_id(emitted)

    def test_markers_classify_kb_content_in_any_tenant(self):
        assert kb_ids.is_kb_chunk_id(f"aws:{TENANT}:kb:{CHUNK}")
        assert kb_ids.is_kb_source_id(toolkit_rewrite(kb_ids.emit_document_id("s3://b/k"), TENANT))
        assert not kb_ids.is_kb_source_id(f"aws:{TENANT}:kb:{CHUNK}")
        assert not kb_ids.is_kb_chunk_id(f"aws:{TENANT}:doc:x")

    def test_toolkit_native_and_foreign_ids_are_not_kb_content(self):
        for native in (f"aws:{TENANT}:abcd1234:ef01", f"aws:{TENANT}:abcd1234:ef01:9999", "aws::abcd1234:ef01"):
            assert not kb_ids.is_kb_chunk_id(native) and not kb_ids.is_kb_source_id(native)
        for foreign in ("bedrock-kb:abc", "toolkit-minted-id", "", "aws:"):
            assert not kb_ids.is_kb_chunk_id(foreign) and not kb_ids.is_kb_source_id(foreign)
            assert kb_ids.bedrock_chunk_id_of(foreign) is None

    def test_cypher_predicate_names_the_marker(self):
        assert "':doc:'" in kb_ids.KB_SOURCE_ID_CYPHER_PREDICATE and "'aws:'" in kb_ids.KB_SOURCE_ID_CYPHER_PREDICATE
