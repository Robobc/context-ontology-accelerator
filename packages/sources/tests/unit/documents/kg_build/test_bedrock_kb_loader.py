# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Bedrock Managed KB chunk loader (fork, INPUT_MODE=BEDROCK_KB).

The Bedrock clients are fakes replaying recorded response shapes (verified
against a live KB on 2026-10-25: ``metadata._chunk_id``, ``documentId``,
``_source_uri``, ``nextToken`` paging, ``ListKnowledgeBaseDocuments`` refusing
SharePoint data sources). ``llama_index`` is installed in the dev venv, so node
ids and SOURCE relationships are asserted on real ``TextNode`` objects: these are
exactly what graphrag-toolkit 3.18.7's ``IdRewriter`` (``aws:`` passthrough),
``ChunkNodeBuilder`` (``chunkId`` = node id) and ``SourceNodeBuilder``
(``sourceId`` = SOURCE relationship id) read.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_sources.documents.kg_build import bedrock_kb_loader as loader
from llama_index.core.schema import NodeRelationship, TextNode

# Mirror graph_build's test module: it stubs graphrag_toolkit before importing.
from . import test_graph_build as _gb  # noqa: F401  (import for its sys.modules stubs)

KB = "71RZ44THV5"
DS_S3 = "IBFPVKIBZU"
DS_SP = "EFLHLJMXMZ"


def _result(chunk_id: str, text: str, doc_id: str, uri: str, ds: str, **extra):
    md = {
        "_chunk_id": chunk_id,
        "_source_uri": uri,
        "_document_title": uri.rsplit("/", 1)[-1],
        "_data_source_id": ds,
        "_excerpt_page_number": 1,
        "_last_updated_at": "2026-10-01T00:00:00Z",
        **extra,
    }
    return {
        "content": {"text": text, "type": "TEXT"},
        "documentId": doc_id,
        "location": {"type": "S3", "s3Location": {"uri": uri}},
        "metadata": md,
        "score": 0.5,
    }


DOC_A = "4#aaaaaaaa-0000-4000-8000-000000000001#" + "a" * 64
DOC_B = "4#bbbbbbbb-0000-4000-8000-000000000002#" + "b" * 64
URI_A = f"s3://bucket/{DS_S3}/guide.docx"
URI_B = f"s3://bucket/{DS_S3}/notes.md"


class FakeRuntime:
    """Replays Retrieve pages keyed by (data_source_id, source_uri|None)."""

    def __init__(self, pages: dict[tuple[str, str | None], list[list[dict]]], throttle_first: int = 0):
        self.pages = pages
        self.calls: list[dict] = []
        self._throttle_left = throttle_first

    @staticmethod
    def _key(kwargs):
        f = kwargs["retrievalConfiguration"]["managedSearchConfiguration"]["filter"]
        if "andAll" in f:
            ds = f["andAll"][0]["equals"]["value"]
            uri = f["andAll"][1]["equals"]["value"]
            return (ds, uri)
        return (f["equals"]["value"], None)

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if self._throttle_left > 0:
            self._throttle_left -= 1
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Retrieve")
        assert kwargs["retrievalConfiguration"]["managedSearchConfiguration"]["numberOfResults"] == 100
        assert "userContext" not in kwargs  # extraction sees everything; ACL is retrieval-time
        pages = self.pages[self._key(kwargs)]
        idx = int(kwargs.get("nextToken", "0"))
        out = {"retrievalResults": pages[idx]}
        if idx + 1 < len(pages):
            out["nextToken"] = str(idx + 1)
        return out


class FakeAgent:
    def __init__(self, s3_docs: dict[str, list[str]] | None = None, refuse: set[str] = frozenset()):
        self.s3_docs = s3_docs or {}
        self.refuse = set(refuse)
        self.list_calls = 0

    def list_knowledge_base_documents(self, **kwargs):
        self.list_calls += 1
        ds = kwargs["dataSourceId"]
        if ds in self.refuse:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "not supported for SHAREPOINTV3"}},
                "ListKnowledgeBaseDocuments",
            )
        return {
            "documentDetails": [
                {"status": "INDEXED", "identifier": {"dataSourceType": "S3", "s3": {"uri": u}}}
                for u in self.s3_docs.get(ds, [])
            ]
        }

    def list_data_sources(self, **kwargs):
        return {"dataSourceSummaries": [{"dataSourceId": DS_S3}, {"dataSourceId": DS_SP}]}


def _s3_fixture(throttle_first: int = 0):
    """Two S3 documents; doc A spans two Retrieve pages with one duplicate chunk."""
    pages = {
        (DS_S3, URI_A): [
            [
                _result("chunkA1", "Alpha one.", DOC_A, URI_A, DS_S3, department="R&D", **{"doc.version": "3"}),
                _result("chunkA2", "Alpha two.", DOC_A, URI_A, DS_S3),
            ],
            [
                _result("chunkA2", "Alpha two.", DOC_A, URI_A, DS_S3),  # duplicate across pages
                _result("chunkA3", "Alpha three.", DOC_A, URI_A, DS_S3),
            ],
        ],
        (DS_S3, URI_B): [[_result("chunkB1", "Beta one.", DOC_B, URI_B, DS_S3)]],
    }
    return FakeRuntime(pages, throttle_first=throttle_first), FakeAgent(s3_docs={DS_S3: [URI_A, URI_B]})


@pytest.mark.unit
class TestIds:
    def test_chunk_id_keeps_bedrock_id_under_aws_prefix(self):
        assert loader.chunk_node_id("lpmmdjblLYZDX5e3xcR8MIXIl3xuyrRVFqSNtTwqBPw") == (
            "aws:lpmmdjblLYZDX5e3xcR8MIXIl3xuyrRVFqSNtTwqBPw"
        )

    def test_document_id_sanitises_hash_separator(self):
        assert loader.document_node_id("4#uuid#sha").startswith("aws:doc:4_uuid_sha")

    @pytest.mark.parametrize("bad", ["", "has space", "slash/no", "q?"])
    def test_ids_the_toolkit_rejects_are_refused(self, bad):
        with pytest.raises(ValueError):
            loader.chunk_node_id(bad)


@pytest.mark.unit
class TestSanitizer:
    def test_collision_disambiguated_and_reported(self):
        seen = []
        out = loader.sanitize_metadata_keys(
            [("report.id", "A"), ("report-id", "B"), ("report_id", "C")], on_collision=lambda **f: seen.append(f)
        )
        assert out == {"report_id": "A", "report_id_2": "B", "report_id_3": "C"}
        assert [s["original_key"] for s in seen] == ["report-id", "report_id"]

    def test_same_key_twice_is_not_a_collision(self):
        assert loader.sanitize_metadata_keys([("a.b", 1), ("a.b", 2)]) == {"a_b": 2}


@pytest.mark.unit
class TestLoader:
    def test_chunks_keep_kb_ids_and_point_at_their_document(self):
        runtime, agent = _s3_fixture()
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent)

        assert [n.id_ for n in res.chunks] == ["aws:chunkA1", "aws:chunkA2", "aws:chunkA3", "aws:chunkB1"]
        assert all(isinstance(n, TextNode) for n in res.chunks)
        assert res.duplicate_chunks_skipped == 1
        # Grouped per document, SOURCE relationship carries the document node id.
        src = res.chunks[0].relationships[NodeRelationship.SOURCE]
        assert src.node_id == loader.document_node_id(DOC_A)
        assert res.chunks[3].relationships[NodeRelationship.SOURCE].node_id == loader.document_node_id(DOC_B)
        assert [d.id_ for d in res.documents] == [loader.document_node_id(DOC_A), loader.document_node_id(DOC_B)]
        # Chunk count equals distinct Retrieve chunks: nothing was split or merged.
        assert len(res.chunks) == 4 and res.manifest[DOC_A] == {"chunkA1", "chunkA2", "chunkA3"}

    def test_metadata_contract(self):
        runtime, agent = _s3_fixture()
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent)
        chunk = res.chunks[0]
        src_md = chunk.relationships[NodeRelationship.SOURCE].metadata
        # Raw identifiers kept as scalar properties; system keys renamed; customer
        # keys sanitised; no key starts with "_" and no collection values.
        assert chunk.metadata["bedrockChunkId"] == "chunkA1"
        assert src_md["bedrockDocumentId"] == DOC_A
        assert src_md["source_uri"] == URI_A and src_md["filename"] == "guide.docx"
        assert src_md["department"] == "R&D" and src_md["doc_version"] == "3"
        assert chunk.metadata["page_number"] == 1
        for md in (chunk.metadata, src_md):
            assert not any(k.startswith("_") for k in md)
            assert not any(isinstance(v, (list, dict)) for v in md.values())
            assert not (set(md) & {"chunkId", "sourceId", "source", "chunk"})
        # Text is the chunk verbatim and metadata stays out of the LLM window.
        assert chunk.text == "Alpha one." and set(chunk.excluded_llm_metadata_keys) == set(chunk.metadata)

    def test_sharepoint_source_falls_back_to_retrieve_sampling(self):
        pages = {
            (DS_SP, None): [
                [_result("c1", "one", DOC_A, "sp://site/a", DS_SP)],
                [_result("c2", "two", DOC_B, "sp://site/b", DS_SP)],
                [_result("c1", "one", DOC_A, "sp://site/a", DS_SP)],  # quiet page 1
                [_result("c2", "two", DOC_B, "sp://site/b", DS_SP)],  # quiet page 2
                [_result("c1", "one", DOC_A, "sp://site/a", DS_SP)],  # quiet page 3 -> stop
                [_result("c9", "never reached", "4#x#y", "sp://site/x", DS_SP)],
            ],
            (DS_SP, "sp://site/a"): [[_result("c1", "one", DOC_A, "sp://site/a", DS_SP)]],
            (DS_SP, "sp://site/b"): [[_result("c2", "two", DOC_B, "sp://site/b", DS_SP)]],
        }
        runtime, agent = FakeRuntime(pages), FakeAgent(refuse={DS_SP})
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_SP], runtime=runtime, agent=agent)
        assert agent.list_calls == 1
        assert res.documents_discovered == 2 and [n.id_ for n in res.chunks] == ["aws:c1", "aws:c2"]

    def test_all_data_sources_when_none_given(self):
        runtime, agent = _s3_fixture()
        runtime.pages[(DS_SP, None)] = [[]]
        agent.refuse.add(DS_SP)
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=None, runtime=runtime, agent=agent)
        assert len(res.chunks) == 4  # S3 source loaded, empty SharePoint source contributed nothing

    def test_document_without_chunks_is_counted_not_emitted(self):
        runtime, agent = _s3_fixture()
        runtime.pages[(DS_S3, URI_B)] = [[]]
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent)
        # URI_B never answered a Retrieve, so it was never discovered as a document.
        assert res.documents_discovered == 1 and res.documents_without_chunks == 0
        runtime2, agent2 = _s3_fixture()
        # Discovered (first page answers) but the full scan yields only empty text.
        runtime2.pages[(DS_S3, URI_B)] = [[_result("cb", "   ", DOC_B, URI_B, DS_S3)]]
        res2 = loader.load_bedrock_kb_chunks(
            knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime2, agent=agent2
        )
        assert res2.documents_without_chunks == 1 and len(res2.documents) == 1

    def test_throttling_is_retried(self, monkeypatch):
        monkeypatch.setattr(loader.time, "sleep", lambda s: None)
        runtime, agent = _s3_fixture(throttle_first=2)
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent)
        assert len(res.chunks) == 4


# ---------------------------------------------------------------------------
# graph_build hooks
# ---------------------------------------------------------------------------


@pytest.fixture()
def gb(monkeypatch):
    def _load(**env):
        _gb._stub_missing_modules()
        for k, v in {**_gb._BASE_ENV, **env}.items():
            monkeypatch.setenv(k, v)
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build

        return graph_build

    return _load


@pytest.mark.unit
class TestGraphBuildHooks:
    def test_defaults_are_todays_behaviour(self, gb):
        mod = gb()
        assert mod.INPUT_MODE == "STAGING" and mod.VECTOR_STORE_MODE == "AOSS"

    def test_invalid_modes_fall_back(self, gb):
        mod = gb(INPUT_MODE="nope", VECTOR_STORE_MODE="s3")
        assert mod.INPUT_MODE == "STAGING" and mod.VECTOR_STORE_MODE == "AOSS"

    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    def test_bedrock_kb_passes_chunking_none(self, _ec, mock_ic, gb):
        mod = gb(INPUT_MODE="BEDROCK_KB", CHUNK_SIZE="512")
        mod._build_indexing_config("b", "ns", "ds", namespace_language="en")
        assert mock_ic.call_args.kwargs["chunking"] is None  # explicit None, not omitted

    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    def test_staging_leaves_chunking_untouched(self, _ec, mock_ic, gb):
        mod = gb(CHUNK_SIZE="0")
        mod._build_indexing_config("b", "ns", "ds", namespace_language="en")
        assert "chunking" not in mock_ic.call_args.kwargs

    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_dummy_vector_store_uri(self, _cfg, gb):
        mod = gb(VECTOR_STORE_MODE="DUMMY")
        _graph, vector = mod._setup_graphrag("tenanta")
        assert vector == "dummy://"
        mod2 = gb(VECTOR_STORE_MODE="AOSS")
        _graph, vector2 = mod2._setup_graphrag("tenanta")
        assert vector2.startswith("aoss://")

    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    def test_health_check_skips_vector_probe_for_dummy(self, mock_gsf, gb):
        mod = gb()
        ddb = MagicMock()
        with patch.dict(
            sys.modules,
            {
                "graphrag_toolkit.lexical_graph.storage.vector": MagicMock(),
                "graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes": MagicMock(),
            },
        ):
            osv = sys.modules["graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes"]
            assert mod._check_backend_health("neptune-db://h:8182", "dummy://", ddb, {"PK": "x"}) is True
            osv.create_os_client.assert_not_called()
        ddb.update.assert_not_called()

    @patch("coa_common.dao.dynamodb.boto3")
    def test_main_dispatches_to_bedrock_loader(self, mock_ddb_boto, gb):
        mod = gb(INPUT_MODE="BEDROCK_KB", BEDROCK_KB_ID=KB)
        table = MagicMock()
        table.get_item.return_value = {"Item": {"status": "ingesting"}}
        mock_ddb_boto.resource.return_value.Table.return_value = table
        with (
            patch.object(mod, "_load_bedrock_kb_documents", return_value=([], 0)) as kb_load,
            patch.object(mod, "_load_staged_documents") as staged,
        ):
            mod.main()  # zero nodes -> "No staged documents found" path, returns cleanly
        kb_load.assert_called_once()
        staged.assert_not_called()

    def test_bedrock_loader_wrapper_assumes_role_and_returns_nodes(self, gb):
        mod = gb(
            INPUT_MODE="BEDROCK_KB",
            BEDROCK_KB_ID=KB,
            BEDROCK_KB_DATA_SOURCE_IDS=f"{DS_S3}, ",
            BEDROCK_KB_EXTRACTION_ROLE_ARN="arn:aws:iam::123456789012:role/kb-extract",
        )
        runtime, agent = _s3_fixture()
        fake_session = MagicMock()
        fake_session.client.side_effect = lambda name: {
            "sts": MagicMock(
                assume_role=MagicMock(
                    return_value={"Credentials": {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}}
                )
            ),
            "bedrock-agent-runtime": runtime,
            "bedrock-agent": agent,
        }[name]
        with patch("boto3.Session", return_value=fake_session) as sess:
            nodes, unloaded = mod._load_bedrock_kb_documents()
        assert [n.id_ for n in nodes] == ["aws:chunkA1", "aws:chunkA2", "aws:chunkA3", "aws:chunkB1"]
        assert unloaded == 0
        # Second Session() carries the assumed-role credentials.
        assert sess.call_args_list[1].kwargs["aws_session_token"] == "t"
