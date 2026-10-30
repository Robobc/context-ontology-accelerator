# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Bedrock Managed KB chunk loader (fork, INPUT_MODE=BEDROCK_KB).

The Bedrock clients are fakes replaying recorded response shapes, verified
against a live Managed KB (``71RZ44THV5``, 2026-10-25 and 2026-10-30):
``metadata._chunk_id``; ``documentId`` is the ``s3://`` URI for an S3 data source
while ``_source_uri`` (the key the KB filters on) is the ``https://`` form;
``nextToken`` paging; ``ListKnowledgeBaseDocuments`` refusing SharePoint data
sources; an ACL-enabled data source returning NOTHING to a ``Retrieve`` without
``userContext`` and only the identity's documents with one. ``llama_index`` is
installed in the dev venv, so node ids and SOURCE relationships are asserted on
real ``TextNode`` objects, and ``TestToolkitIdContract`` runs graphrag-toolkit
3.18.7's real ``TenantId.rewrite_id`` on the emitted ids: the build stage
rewrites segment 1 of every id to the tenant, which is why ids are emitted as
``aws::kb:<id>`` / ``aws::doc:<id>`` and never ``aws:<id>``.
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


# S3 data source, live shape: documentId == the s3:// URI the listing returns;
# _source_uri (what the KB filters on) is the https:// form of the same object.
DOC_A = f"s3://bucket/{DS_S3}/guide.docx"
DOC_B = f"s3://bucket/{DS_S3}/notes.md"
URI_A = f"https://bucket.s3.eu-central-1.amazonaws.com/{DS_S3}/guide.docx"
URI_B = f"https://bucket.s3.eu-central-1.amazonaws.com/{DS_S3}/notes.md"
# SharePoint data source, live shape: documentId is 4#<uuid>#<sha256>.
SP_DOC_A = "4#aaaaaaaa-0000-4000-8000-000000000001#" + "a" * 64
SP_DOC_B = "4#bbbbbbbb-0000-4000-8000-000000000002#" + "b" * 64
EXTRACTOR = "extractor@example.com"


class FakeRuntime:
    """Replays Retrieve pages keyed by (data_source_id, source_uri|None).

    ``acl`` maps a data source id to ``{user: {documentId, ...}}``: such a data
    source answers like a live ACL-enabled one (nothing without ``userContext``,
    only the user's documents with one). Data sources not in ``acl`` answer every
    identity, like a non-ACL one.
    """

    def __init__(
        self,
        pages: dict[tuple[str, str | None], list[list[dict]]],
        throttle_first: int = 0,
        acl: dict[str, dict[str, set[str]]] | None = None,
    ):
        self.pages = pages
        self.calls: list[dict] = []
        self._throttle_left = throttle_first
        self.acl = acl or {}

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
        key = self._key(kwargs)
        pages = self.pages.get(key, [[]])
        idx = int(kwargs.get("nextToken", "0"))
        results = pages[idx]
        if key[0] in self.acl:
            user = (kwargs.get("userContext") or {}).get("userId")
            visible = self.acl[key[0]].get(user, set()) if user else set()
            results = [r for r in results if r["documentId"] in visible]
        out = {"retrievalResults": results}
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


def _s3_fixture(throttle_first: int = 0, acl: dict | None = None):
    """Two S3 documents; doc A spans two Retrieve pages with one duplicate chunk.

    The listed path discovers documents through the data-source-wide scan
    (``(DS_S3, None)``) and reads each document's chunks through its
    ``_source_uri`` filter.
    """
    pages = {
        (DS_S3, None): [
            [
                # Customer metadata attributes are per document in the KB, so every
                # chunk of A carries them, including the one the scan sees first.
                _result("chunkA1", "Alpha one.", DOC_A, URI_A, DS_S3, department="R&D", **{"doc.version": "3"}),
                _result("chunkB1", "Beta one.", DOC_B, URI_B, DS_S3),
            ]
        ],
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
    return FakeRuntime(pages, throttle_first=throttle_first, acl=acl), FakeAgent(s3_docs={DS_S3: [DOC_A, DOC_B]})


@pytest.mark.unit
class TestIds:
    def test_chunk_id_is_the_bedrock_id_in_the_toolkit_tenant_shape(self):
        # Empty segment 1: the toolkit's build stage overwrites it with the tenant.
        assert loader.chunk_node_id("lpmmdjblLYZDX5e3xcR8MIXIl3xuyrRVFqSNtTwqBPw") == (
            "aws::kb:lpmmdjblLYZDX5e3xcR8MIXIl3xuyrRVFqSNtTwqBPw"
        )

    def test_document_id_keeps_a_sharepoint_id_readable(self):
        assert loader.document_node_id("4#uuid#sha") == "aws::doc:4_uuid_sha"

    def test_document_id_hashes_an_s3_uri(self):
        # S3 documentIds are s3:// URIs: '/' and ':' are outside the id grammar.
        out = loader.document_node_id(DOC_A)
        assert out.startswith("aws::doc:h.") and len(out) == len("aws::doc:h.") + 32
        assert out == loader.document_node_id(DOC_A) != loader.document_node_id(DOC_B)

    @pytest.mark.parametrize("bad", ["", "has space", "slash/no", "q?"])
    def test_chunk_ids_outside_the_grammar_are_refused(self, bad):
        with pytest.raises(ValueError):
            loader.chunk_node_id(bad)


@pytest.mark.unit
class TestToolkitIdContract:
    """The real toolkit rewrite, on the real emitted ids: the pin for the id-collapse bug."""

    TENANT = "550e8400e29b41d4a71644665"

    def _rewrite(self, node_id: str) -> str:
        from graphrag_toolkit.lexical_graph import TenantId

        return TenantId(self.TENANT).rewrite_id(node_id)

    def test_chunk_and_document_ids_survive_the_tenant_rewrite(self):
        from coa_common.kb_ids import bedrock_chunk_id_of, is_kb_chunk_id, is_kb_source_id, stored_chunk_id

        chunk = loader.chunk_node_id("Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw")
        stored = self._rewrite(chunk)
        assert stored == f"aws:{self.TENANT}:kb:Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw"
        assert stored == stored_chunk_id(self.TENANT, "Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw")
        assert is_kb_chunk_id(stored) and bedrock_chunk_id_of(stored) == "Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw"
        for doc in (SP_DOC_A, DOC_A):
            stored_doc = self._rewrite(loader.document_node_id(doc))
            assert stored_doc.startswith(f"aws:{self.TENANT}:doc:") and is_kb_source_id(stored_doc)

    def test_a_two_segment_id_would_collapse(self):
        # What the first implementation emitted; every chunk became this one node.
        assert self._rewrite("aws:Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw") == f"aws:{self.TENANT}:"

    def test_toolkit_native_ids_are_not_mistaken_for_kb_content(self):
        from coa_common.kb_ids import is_kb_chunk_id, is_kb_source_id

        source = self._rewrite("aws::abcd1234:ef01")
        chunk = self._rewrite("aws::abcd1234:ef01:9999")
        assert not is_kb_source_id(source) and not is_kb_chunk_id(chunk)


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

        assert [n.id_ for n in res.chunks] == [
            "aws::kb:chunkA1",
            "aws::kb:chunkA2",
            "aws::kb:chunkA3",
            "aws::kb:chunkB1",
        ]
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
        # source_uri is the KB's _source_uri (https for S3), byte-equal to what the
        # serve gate will filter Retrieve on; the raw documentId rides as a property.
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
                [_result("c1", "one", SP_DOC_A, "sp://site/a", DS_SP)],
                [_result("c2", "two", SP_DOC_B, "sp://site/b", DS_SP)],
                [_result("c1", "one", SP_DOC_A, "sp://site/a", DS_SP)],  # quiet page 1
                [_result("c2", "two", SP_DOC_B, "sp://site/b", DS_SP)],  # quiet page 2
                [_result("c1", "one", SP_DOC_A, "sp://site/a", DS_SP)],  # quiet page 3 -> stop
                [_result("c9", "never reached", "4#x#y", "sp://site/x", DS_SP)],
            ],
            (DS_SP, "sp://site/a"): [[_result("c1", "one", SP_DOC_A, "sp://site/a", DS_SP)]],
            (DS_SP, "sp://site/b"): [[_result("c2", "two", SP_DOC_B, "sp://site/b", DS_SP)]],
        }
        runtime, agent = FakeRuntime(pages), FakeAgent(refuse={DS_SP})
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_SP], runtime=runtime, agent=agent)
        assert agent.list_calls == 1
        assert res.documents_discovered == 2 and [n.id_ for n in res.chunks] == ["aws::kb:c1", "aws::kb:c2"]

    def test_list_denied_is_a_failure_not_a_fallback(self):
        """Only the SharePoint ValidationException means 'sample instead'; an
        AccessDenied on the listing would recur on the scan and must surface."""
        runtime, agent = _s3_fixture()

        def denied(**kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListKnowledgeBaseDocuments"
            )

        agent.list_knowledge_base_documents = denied
        with pytest.raises(ClientError, match="AccessDeniedException"):
            loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent)
        assert runtime.calls == []  # nothing was sampled on the same broken credentials

    def test_all_data_sources_when_none_given(self):
        runtime, agent = _s3_fixture()
        runtime.pages[(DS_SP, None)] = [[]]
        agent.refuse.add(DS_SP)
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=None, runtime=runtime, agent=agent)
        assert len(res.chunks) == 4  # S3 source loaded, empty SharePoint source contributed nothing

    def test_document_without_chunks_is_counted_not_emitted(self):
        runtime, agent = _s3_fixture()
        # Listed and seen by the scan, but its own chunk scan answers nothing.
        runtime.pages[(DS_S3, URI_B)] = [[]]
        res = loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent)
        assert res.documents_discovered == 2 and res.documents_without_chunks == 1 and len(res.documents) == 1
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


@pytest.mark.unit
class TestExtractionIdentity:
    """ACL-enabled data sources answer the identity on the call, and only that; mirrors the live KB."""

    def test_identity_is_passed_on_every_retrieve_and_the_graph_is_its_view(self):
        runtime, agent = _s3_fixture(acl={DS_S3: {EXTRACTOR: {DOC_A, DOC_B}}})
        res = loader.load_bedrock_kb_chunks(
            knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent, extraction_user_id=EXTRACTOR
        )
        assert len(res.chunks) == 4 and res.documents_not_visible == 0
        assert all(c["userContext"] == {"userId": EXTRACTOR} for c in runtime.calls)

    def test_acl_source_without_identity_fails_rather_than_building_an_empty_graph(self):
        runtime, agent = _s3_fixture(acl={DS_S3: {EXTRACTOR: {DOC_A, DOC_B}}})
        with pytest.raises(loader.ExtractionIdentityRequiredError, match="extractionUserId"):
            loader.load_bedrock_kb_chunks(knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent)
        assert all("userContext" not in c for c in runtime.calls)

    def test_identity_without_any_access_fails_the_same_way_naming_it(self):
        runtime, agent = _s3_fixture(acl={DS_S3: {EXTRACTOR: {DOC_A, DOC_B}}})
        with pytest.raises(loader.ExtractionIdentityRequiredError, match="nobody@example.com"):
            loader.load_bedrock_kb_chunks(
                knowledge_base_id=KB,
                data_source_ids=[DS_S3],
                runtime=runtime,
                agent=agent,
                extraction_user_id="nobody@example.com",
            )

    def test_partial_entitlement_is_the_owners_choice_and_is_reported(self):
        # The identity may read A but not B: B is listed, invisible, and not in the graph.
        runtime, agent = _s3_fixture(acl={DS_S3: {EXTRACTOR: {DOC_A}}})
        res = loader.load_bedrock_kb_chunks(
            knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent, extraction_user_id=EXTRACTOR
        )
        assert [n.id_ for n in res.chunks] == ["aws::kb:chunkA1", "aws::kb:chunkA2", "aws::kb:chunkA3"]
        assert res.documents_not_visible == 1 and res.documents_discovered == 1

    def test_non_acl_source_ignores_the_identity(self):
        runtime, agent = _s3_fixture()  # no acl: answers everyone
        res = loader.load_bedrock_kb_chunks(
            knowledge_base_id=KB, data_source_ids=[DS_S3], runtime=runtime, agent=agent, extraction_user_id=EXTRACTOR
        )
        assert len(res.chunks) == 4

    def test_coverage_check_runs_as_the_identity(self):
        runtime = MagicMock()
        runtime.get_document_content.return_value = {"presignedUrl": "https://x"}
        with patch.object(loader, "urllib", create=True), patch("urllib.request.urlopen") as uo:
            uo.return_value.__enter__.return_value.read.return_value = (
                b'{"nonPlainTextDocumentContent":[{"text":"Alpha one."}]}'
            )
            ratio = loader.coverage_ratio(
                runtime,
                knowledge_base_id=KB,
                data_source_id=DS_S3,
                document_id=DOC_A,
                chunk_texts=["Alpha one."],
                extraction_user_id=EXTRACTOR,
            )
        assert ratio == 1.0
        assert runtime.get_document_content.call_args.kwargs["userContext"] == {"userId": EXTRACTOR}


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

    def test_bedrock_loader_wrapper_assumes_role_with_namespace_external_id(self, gb, monkeypatch):
        """The extraction role is customer-supplied, so the assume MUST carry the
        namespace-derived ExternalId (confused-deputy guard), through the same
        helper every other datasource assume uses."""
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        role = "arn:aws:iam::123456789012:role/coa-dev-datasource-access-kb"
        mod = gb(
            INPUT_MODE="BEDROCK_KB",
            BEDROCK_KB_ID=KB,
            BEDROCK_KB_DATA_SOURCE_IDS=f"{DS_S3}, ",
            BEDROCK_KB_CROSS_ACCOUNT_ROLE_ARN=role,
        )
        runtime, agent = _s3_fixture()
        assumed = MagicMock()
        assumed.client.side_effect = lambda name: {"bedrock-agent-runtime": runtime, "bedrock-agent": agent}[name]
        with patch(
            "coa_sources.database.connectors.sts_assume.assume_datasource_session", return_value=assumed
        ) as assume:
            nodes, unloaded = mod._load_bedrock_kb_documents("11111111-1111-4111-8111-111111111111")
        assert [n.id_ for n in nodes] == ["aws::kb:chunkA1", "aws::kb:chunkA2", "aws::kb:chunkA3", "aws::kb:chunkB1"]
        assert unloaded == 0
        assert assume.call_args.args[:2] == (role, "coa-dev-11111111-1111-4111-8111-111111111111")
        assert assume.call_args.kwargs["session_name"].startswith("coa-kg-build-")

    def test_bedrock_loader_wrapper_without_role_uses_task_credentials(self, gb):
        mod = gb(INPUT_MODE="BEDROCK_KB", BEDROCK_KB_ID=KB, BEDROCK_KB_CROSS_ACCOUNT_ROLE_ARN="")
        runtime, agent = _s3_fixture()
        fake_session = MagicMock()
        fake_session.client.side_effect = lambda name: {"bedrock-agent-runtime": runtime, "bedrock-agent": agent}[name]
        with (
            patch("boto3.Session", return_value=fake_session),
            patch("coa_sources.database.connectors.sts_assume.assume_datasource_session") as assume,
        ):
            nodes, _ = mod._load_bedrock_kb_documents("11111111-1111-4111-8111-111111111111")
        assert len(nodes) == 4
        assume.assert_not_called()

    def test_bedrock_loader_wrapper_reads_the_extraction_identity_from_the_task_env(self, gb):
        mod = gb(
            INPUT_MODE="BEDROCK_KB",
            BEDROCK_KB_ID=KB,
            BEDROCK_KB_DATA_SOURCE_IDS=DS_S3,
            BEDROCK_KB_CROSS_ACCOUNT_ROLE_ARN="",
            BEDROCK_KB_EXTRACTION_USER_ID=f" {EXTRACTOR} ",
        )
        runtime, agent = _s3_fixture(acl={DS_S3: {EXTRACTOR: {DOC_A, DOC_B}}})
        fake_session = MagicMock()
        fake_session.client.side_effect = lambda name: {"bedrock-agent-runtime": runtime, "bedrock-agent": agent}[name]
        with patch("boto3.Session", return_value=fake_session):
            nodes, _ = mod._load_bedrock_kb_documents("11111111-1111-4111-8111-111111111111")
        assert len(nodes) == 4
        assert all(c["userContext"] == {"userId": EXTRACTOR} for c in runtime.calls)

    def test_bedrock_loader_wrapper_surfaces_the_identity_error_on_the_source_row(self, gb, mock_ddb_boto=None):
        """The SCAN_FAILED message names extractionUserId: the fix is the steward's, not the operator's."""
        mod = gb(INPUT_MODE="BEDROCK_KB", BEDROCK_KB_ID=KB, BEDROCK_KB_DATA_SOURCE_IDS=DS_S3)
        runtime, agent = _s3_fixture(acl={DS_S3: {EXTRACTOR: {DOC_A, DOC_B}}})
        fake_session = MagicMock()
        fake_session.client.side_effect = lambda name: {"bedrock-agent-runtime": runtime, "bedrock-agent": agent}[name]
        with patch("boto3.Session", return_value=fake_session), pytest.raises(Exception) as exc_info:
            mod._load_bedrock_kb_documents("11111111-1111-4111-8111-111111111111")
        assert type(exc_info.value).__name__ == "ExtractionIdentityRequiredError"
        assert "extractionUserId" in str(exc_info.value)
