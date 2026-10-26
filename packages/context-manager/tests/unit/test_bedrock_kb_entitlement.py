# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fork: per-user Bedrock KB retrieval and the document entitlement gate.

Covers the retrieval half of the Bedrock Managed KB chunk source: Retrieve hits
carry the graph's ``aws:<_chunk_id>`` ids, every call names the caller
(``userContext``) and fails closed without one, and graph-derived text (adjacent
chunks, supporting statements) is withheld for documents the KB would not show
this user. The ACL fixture mirrors the spec's acceptance case: alice may read
finance, carol has no entitlement at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_serve.request_identity import get_request_user, reset_request_user, set_request_user
from coa_serve.tier3.bedrock_kb_retriever import BedrockKnowledgeBaseRetriever
from coa_serve.tier3.document_entitlement import (
    CHUNK_PROVENANCE,
    ENTITY_PROVENANCE,
    SOURCE_PROVENANCE,
    STATEMENT_PROVENANCE,
    DocumentEntitlementGate,
    kb_retriever_resolver,
)
from coa_serve.tier3.document_retriever import RoutingDocumentRetriever
from coa_serve.tier3.vector_retriever import ChunkResult

KB = "ABCDEFGHIJ"
FINANCE = "sp://site/finance/q3.docx"
HR = "sp://site/hr/salaries.docx"
SHARED = "sp://site/shared/handbook.docx"
# Which documents each user may read, per the KB's ACL.
ACL = {"alice@example.com": {FINANCE, SHARED}, "bob@example.com": {HR, SHARED}, "carol@example.com": set()}


def _hit(chunk_id: str, text: str, uri: str) -> dict:
    return {
        "content": {"text": text},
        "documentId": "4#doc#sha",
        "location": {"type": "SHAREPOINT", "sharePointLocation": {"url": uri}},
        "metadata": {"_chunk_id": chunk_id, "_source_uri": uri, "_document_title": uri.rsplit("/", 1)[-1]},
        "score": 0.7,
    }


class AclKbClient:
    """A Bedrock runtime client that answers per userContext like an ACL-enabled KB."""

    def __init__(self):
        self.calls: list[dict] = []
        self.docs = {FINANCE: ("fin1", "Q3 revenue grew."), HR: ("hr1", "Salary bands."), SHARED: ("sh1", "Handbook.")}

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        user = (kwargs.get("userContext") or {}).get("userId")
        allowed = ACL.get(user, set()) if user else set(self.docs)  # no userContext = extraction view
        cfg = kwargs["retrievalConfiguration"]
        search = cfg.get("managedSearchConfiguration") or cfg.get("vectorSearchConfiguration")
        flt = search.get("filter")
        uris = [flt["equals"]["value"]] if flt and "equals" in flt else list(self.docs)
        results = [_hit(*self.docs[u], u) for u in uris if u in allowed and u in self.docs]
        return {"retrievalResults": results[: search["numberOfResults"]]}


@pytest.fixture()
def alice():
    token = set_request_user("alice@example.com")
    yield "alice@example.com"
    reset_request_user(token)


@pytest.mark.unit
class TestRetriever:
    @pytest.mark.asyncio
    async def test_hits_carry_graph_ids_and_source_uri(self):
        client = AclKbClient()
        r = BedrockKnowledgeBaseRetriever(KB, client=client)  # today's defaults: vector, no userContext
        chunks = await r.retrieve("revenue", "ns", top_k=10)
        assert sorted(c.chunk_id for c in chunks) == ["aws:fin1", "aws:hr1", "aws:sh1"]
        assert {c.source_doc for c in chunks} == {FINANCE, HR, SHARED}
        assert "vectorSearchConfiguration" in client.calls[0]["retrievalConfiguration"]
        assert "userContext" not in client.calls[0]

    @pytest.mark.asyncio
    async def test_sha_fallback_only_without_a_chunk_id(self):
        client = MagicMock()
        client.retrieve.return_value = {
            "retrievalResults": [{"content": {"text": "x"}, "metadata": {"source_uri": "s3://b/k"}, "score": 0.1}]
        }
        chunks = await BedrockKnowledgeBaseRetriever(KB, client=client).retrieve("q", "ns")
        assert chunks[0].chunk_id.startswith("bedrock-kb:")

    @pytest.mark.asyncio
    async def test_managed_mode_with_user_context(self, alice):
        client = AclKbClient()
        r = BedrockKnowledgeBaseRetriever(KB, client=client, search_mode="managed", enforce_user_context=True)
        chunks = await r.retrieve("anything", "ns", top_k=10)
        call = client.calls[0]
        assert "managedSearchConfiguration" in call["retrievalConfiguration"]
        assert call["userContext"] == {"userId": alice}
        assert sorted(c.chunk_id for c in chunks) == ["aws:fin1", "aws:sh1"]  # alice: finance + shared

    @pytest.mark.asyncio
    async def test_explicit_user_id_overrides_request_identity(self, alice):
        client = AclKbClient()
        r = BedrockKnowledgeBaseRetriever(KB, client=client, enforce_user_context=True)
        chunks = await r.retrieve("anything", "ns", user_id="bob@example.com")
        assert client.calls[0]["userContext"] == {"userId": "bob@example.com"}
        assert sorted(c.chunk_id for c in chunks) == ["aws:hr1", "aws:sh1"]

    @pytest.mark.asyncio
    async def test_enforced_without_caller_returns_nothing_and_never_calls(self):
        assert get_request_user() == ""
        client = AclKbClient()
        r = BedrockKnowledgeBaseRetriever(KB, client=client, enforce_user_context=True)
        assert await r.retrieve("anything", "ns") == []
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_carol_sees_nothing(self):
        client = AclKbClient()
        r = BedrockKnowledgeBaseRetriever(KB, client=client, enforce_user_context=True)
        assert await r.retrieve("anything", "ns", user_id="carol@example.com") == []

    @pytest.mark.asyncio
    async def test_entitled_source_uris_probe(self, alice):
        client = AclKbClient()
        r = BedrockKnowledgeBaseRetriever(KB, client=client, search_mode="managed", enforce_user_context=True)
        got = await r.entitled_source_uris([FINANCE, HR, SHARED, ""])
        assert got == {FINANCE, SHARED}
        for call in client.calls:
            search = call["retrievalConfiguration"]["managedSearchConfiguration"]
            assert search["numberOfResults"] == 1 and search["filter"]["equals"]["key"] == "_source_uri"
            assert call["userContext"] == {"userId": alice}

    @pytest.mark.asyncio
    async def test_entitlement_probe_fails_closed(self):
        client = AclKbClient()
        r = BedrockKnowledgeBaseRetriever(KB, client=client, enforce_user_context=True)
        assert await r.entitled_source_uris([FINANCE]) == set()  # no caller
        r2 = BedrockKnowledgeBaseRetriever(KB, client=client, enforce_user_context=False)
        assert await r2.entitled_source_uris([FINANCE], user_id="alice@example.com") == set()  # not per-user

    @pytest.mark.asyncio
    async def test_probe_distinguishes_denied_from_unresolved(self, alice):
        client = AclKbClient()
        real = client.retrieve

        def retrieve(**kwargs):
            if kwargs["retrievalConfiguration"]["managedSearchConfiguration"]["filter"]["equals"]["value"] == SHARED:
                raise RuntimeError("ThrottlingException")
            return real(**kwargs)

        client.retrieve = retrieve
        r = BedrockKnowledgeBaseRetriever(KB, client=client, search_mode="managed", enforce_user_context=True)
        assert await r.probe_source_uris([FINANCE, HR, SHARED]) == {FINANCE: True, HR: False, SHARED: None}
        assert await r.entitled_source_uris([FINANCE, HR, SHARED]) == {FINANCE}

    def test_search_mode_validated(self):
        with pytest.raises(ValueError):
            BedrockKnowledgeBaseRetriever(KB, search_mode="hybrid")


def _gate(retriever, rows_by_query: dict[str, list[dict]]):
    """A gate whose Cypher runner replays canned rows (keyed by a template fragment)."""
    gate = DocumentEntitlementGate("neptune-db://h:8182", lambda ns: retriever, cache_ttl_s=60)
    gate.cypher_calls: list[str] = []  # type: ignore[attr-defined]

    async def _run(cypher, params):
        gate.cypher_calls.append(cypher)  # type: ignore[attr-defined]
        for key, rows in rows_by_query.items():
            if key in cypher:
                return rows
        return []

    gate._run_cypher = _run  # type: ignore[method-assign]
    gate._label = staticmethod(lambda ns, name: f"`{name}`")  # type: ignore[method-assign]
    return gate


def _per_user_retriever():
    return BedrockKnowledgeBaseRetriever(KB, client=AclKbClient(), search_mode="managed", enforce_user_context=True)


@pytest.mark.unit
class TestGate:
    @pytest.mark.asyncio
    async def test_chunks_from_unentitled_documents_are_dropped(self, alice):
        gate = _gate(
            _per_user_retriever(),
            {
                "__EXTRACTED_FROM__`]->(s:": [
                    {"chunkId": "aws:fin2", "uri": FINANCE},
                    {"chunkId": "aws:hr2", "uri": HR},
                    {"chunkId": "aws:sh2", "uri": SHARED},
                ]
            },
        )
        chunks = [
            ChunkResult("aws:fin2", "finance text", "", 1.0),
            ChunkResult("aws:hr2", "hr text", "", 1.0),
            ChunkResult("aws:sh2", "shared text", "", 1.0),
            ChunkResult("aws:orphan", "no provenance", "", 1.0),
            ChunkResult("toolkit-minted-id", "not from the KB", "", 1.0),
        ]
        kept = await gate.filter_chunks(chunks, "ns")
        assert [c.chunk_id for c in kept] == ["aws:fin2", "aws:sh2", "toolkit-minted-id"]

    @pytest.mark.asyncio
    async def test_carol_gets_no_kb_chunks(self):
        gate = _gate(_per_user_retriever(), {"__EXTRACTED_FROM__`]->(s:": [{"chunkId": "aws:fin2", "uri": FINANCE}]})
        kept = await gate.filter_chunks([ChunkResult("aws:fin2", "t", "", 1.0)], "ns", user_id="carol@example.com")
        assert kept == []

    @pytest.mark.asyncio
    async def test_no_caller_keeps_nothing_from_the_kb(self):
        assert get_request_user() == ""
        gate = _gate(_per_user_retriever(), {"__EXTRACTED_FROM__`]->(s:": [{"chunkId": "aws:fin2", "uri": FINANCE}]})
        assert await gate.filter_chunks([ChunkResult("aws:fin2", "t", "", 1.0)], "ns") == []

    @pytest.mark.asyncio
    async def test_statements_need_one_entitled_supporting_document(self, alice):
        gate = _gate(
            _per_user_retriever(),
            {
                "__SUPPORTS__": [
                    {"entityId": "e1", "text": "Revenue grew 8%.", "uri": FINANCE},
                    {"entityId": "e1", "text": "Band 4 pays X.", "uri": HR},
                    {"entityId": "e2", "text": "Both docs say this.", "uri": HR},
                    {"entityId": "e2", "text": "Both docs say this.", "uri": SHARED},
                ]
            },
        )
        chunks = [
            ChunkResult("e1#stmt0", "Revenue grew 8%.", "", 1.0, uri="e1"),
            ChunkResult("e1#stmt1", "Band 4 pays X.", "", 1.0, uri="e1"),
            ChunkResult("e2#stmt0", "Both docs say this.", "", 1.0, uri="e2"),
            ChunkResult("e3#stmt0", "Unknown provenance.", "", 1.0, uri="e3"),
        ]
        kept = await gate.filter_statement_chunks(chunks, "ns")
        assert [c.text for c in kept] == ["Revenue grew 8%.", "Both docs say this."]
        assert "__MENTIONED_IN__" in gate.cypher_calls[0] and "__EXTRACTED_FROM__" in gate.cypher_calls[0]

    @pytest.mark.asyncio
    async def test_passthrough_when_namespace_is_not_a_per_user_kb(self):
        plain = BedrockKnowledgeBaseRetriever(KB, client=AclKbClient())  # enforce_user_context=False
        gate = _gate(plain, {})
        chunks = [ChunkResult("aws:fin2", "t", "", 1.0)]
        assert await gate.filter_chunks(chunks, "ns") is chunks
        assert not gate.applies_to("ns") and gate.cypher_calls == []
        gate_none = _gate(None, {})
        assert await gate_none.filter_statement_chunks(chunks, "ns") is chunks

    @pytest.mark.asyncio
    async def test_entitlement_answers_are_cached_per_user(self, alice):
        retriever = _per_user_retriever()
        gate = _gate(retriever, {"__EXTRACTED_FROM__`]->(s:": [{"chunkId": "aws:fin2", "uri": FINANCE}]})
        chunks = [ChunkResult("aws:fin2", "t", "", 1.0)]
        await gate.filter_chunks(chunks, "ns")
        await gate.filter_chunks(chunks, "ns")
        probes = [
            c
            for c in retriever._client.calls
            if c["retrievalConfiguration"]["managedSearchConfiguration"]["numberOfResults"] == 1
        ]
        assert len(probes) == 1  # second call answered from cache
        await gate.filter_chunks(chunks, "ns", user_id="bob@example.com")
        assert len([c for c in retriever._client.calls if c.get("userContext") == {"userId": "bob@example.com"}]) == 1

    def test_templates_resolve_provenance_via_source_uri(self):
        assert "s.source_uri AS uri" in CHUNK_PROVENANCE and "__EXTRACTED_FROM__" in CHUNK_PROVENANCE
        for edge in ("__SUBJECT__", "__SUPPORTS__", "__MENTIONED_IN__", "__EXTRACTED_FROM__"):
            assert edge in STATEMENT_PROVENANCE and edge in ENTITY_PROVENANCE
        assert "id(s) AS sourceId" in SOURCE_PROVENANCE

    @pytest.mark.asyncio
    async def test_entities_need_one_entitled_source_document(self, alice):
        from coa_serve.tier3.graph_traverser import GraphEntity

        gate = _gate(
            _per_user_retriever(),
            {
                "RETURN DISTINCT id(e) AS entityId": [
                    {"entityId": "e1", "uri": FINANCE},
                    {"entityId": "e2", "uri": HR},
                    {"entityId": "e3", "uri": HR},
                    {"entityId": "e3", "uri": SHARED},
                ]
            },
        )
        entities = [
            GraphEntity(uri="e1", label="Q3 Revenue", type="T"),
            GraphEntity(uri="e2", label="Band 4", type="T"),  # HR only: withheld
            GraphEntity(uri="e3", label="Handbook", type="T"),  # HR + shared: one entitled is enough
            GraphEntity(uri="e4", label="Orphan", type="T"),  # no provenance: withheld
        ]
        kept = await gate.filter_entities(entities, "ns")
        assert [e.label for e in kept] == ["Q3 Revenue", "Handbook"]
        assert await gate.filter_entities(entities, "ns", user_id="carol@example.com") == []

    @pytest.mark.asyncio
    async def test_lexical_nodes_are_gated_by_their_source_document(self, alice):
        from types import SimpleNamespace

        gate = _gate(
            _per_user_retriever(),
            {
                "WHERE id(s) IN $source_ids": [
                    {"sourceId": "aws:doc:fin", "uri": FINANCE},
                    {"sourceId": "aws:doc:hr", "uri": HR},
                ]
            },
        )

        def node(text, source):
            meta = {"source": {"sourceId": source}} if source is not None else {}
            return SimpleNamespace(node_id=text, text=text, score=0.5, metadata=meta)

        nodes = [
            node("finance text", "aws:doc:fin"),
            node("hr text", "aws:doc:hr"),
            node("staged text", "7f3a-toolkit-hash"),  # not a KB source: passes through
            node("unplaceable text", None),  # no provenance: fail closed
            node("orphan kb text", "aws:doc:unknown"),  # KB source the graph does not know: fail closed
        ]
        kept = await gate.filter_lexical_nodes(nodes, "ns")
        assert [n.text for n in kept] == ["finance text", "staged text"]
        assert "hr text" not in [n.text for n in kept]
        assert await gate.filter_lexical_nodes(nodes, "ns", user_id="carol@example.com") == [nodes[2]]

    @pytest.mark.asyncio
    async def test_unresolved_probe_withholds_without_caching_a_denial(self, alice):
        """A throttled KB must not become a 60 s 'not entitled' answer."""
        retriever = _per_user_retriever()
        flaky = {"fail": True}
        real_retrieve = retriever._client.retrieve

        def retrieve(**kwargs):
            if flaky["fail"] and kwargs["retrievalConfiguration"]["managedSearchConfiguration"]["numberOfResults"] == 1:
                raise RuntimeError("ThrottlingException")
            return real_retrieve(**kwargs)

        retriever._client.retrieve = retrieve
        gate = _gate(retriever, {"__EXTRACTED_FROM__`]->(s:": [{"chunkId": "aws:fin2", "uri": FINANCE}]})
        chunks = [ChunkResult("aws:fin2", "t", "", 1.0)]
        assert await gate.filter_chunks(chunks, "ns") == []  # withheld for this request
        assert gate._cache == {}  # but not remembered as a denial
        flaky["fail"] = False
        assert [c.chunk_id for c in await gate.filter_chunks(chunks, "ns")] == ["aws:fin2"]

    @pytest.mark.asyncio
    async def test_cache_is_bounded(self, alice):
        retriever = _per_user_retriever()
        retriever._client.docs = {f"sp://d/{i}": (f"c{i}", "t") for i in range(6)}
        gate = _gate(retriever, {})
        gate._cache_max_entries = 4
        await gate.entitled_uris("ns", list(retriever._client.docs))
        assert len(gate._cache) <= 4


@pytest.mark.unit
class TestResolver:
    def test_routing_retriever_resolves_per_namespace(self):
        per_user = _per_user_retriever()
        plain = BedrockKnowledgeBaseRetriever(KB, client=AclKbClient())
        routing = RoutingDocumentRetriever(default=MagicMock(), routes={"docs": per_user, "legacy": plain})
        resolve = kb_retriever_resolver(routing)
        assert resolve is not None
        assert resolve("docs") is per_user
        assert resolve("legacy") is None and resolve("other") is None

    def test_none_when_no_per_user_kb_is_configured(self):
        plain = BedrockKnowledgeBaseRetriever(KB, client=AclKbClient())
        assert kb_retriever_resolver(RoutingDocumentRetriever(default=MagicMock(), routes={"a": plain})) is None
        assert kb_retriever_resolver(MagicMock(spec=[])) is None

    def test_bare_per_user_retriever_applies_everywhere(self):
        per_user = _per_user_retriever()
        resolve = kb_retriever_resolver(per_user)
        assert resolve is not None and resolve("any") is per_user


@pytest.mark.unit
class TestToolWiring:
    @pytest.mark.asyncio
    async def test_chunk_tool_filters_through_the_gate(self):
        from coa_serve.tier3.agentic.tools.chunk_tool import ChunkExpansionTool

        gate = MagicMock()

        async def _filter(chunks, namespace, user_id=None):
            assert user_id == "alice@example.com"
            return [c for c in chunks if c.chunk_id != "aws:hr9"]

        gate.filter_chunks = _filter
        tool = ChunkExpansionTool("neptune-db://h:8182", entitlement_gate=gate)

        async def _rows(cypher, params):
            return [{"chunkId": "aws:fin9", "text": "fin"}, {"chunkId": "aws:hr9", "text": "hr"}]

        tool._run_cypher = _rows  # type: ignore[method-assign]
        tool._chunk_label = staticmethod(lambda ns: "`__Chunk__`")  # type: ignore[method-assign]
        result = await tool.invoke(namespace="ns", chunk_ids=["aws:x"], profile={"email": "alice@example.com"})
        assert [c.chunk_id for c in result.chunks] == ["aws:fin9"]

    @pytest.mark.asyncio
    async def test_chunk_tool_fails_closed_when_gate_errors(self):
        from coa_serve.tier3.agentic.tools.chunk_tool import ChunkExpansionTool

        gate = MagicMock()

        async def _boom(chunks, namespace, user_id=None):
            raise RuntimeError("neptune down")

        gate.filter_chunks = _boom
        tool = ChunkExpansionTool("neptune-db://h:8182", entitlement_gate=gate)

        async def _rows(cypher, params):
            return [{"chunkId": "aws:fin9", "text": "fin"}]

        tool._run_cypher = _rows  # type: ignore[method-assign]
        tool._chunk_label = staticmethod(lambda ns: "`__Chunk__`")  # type: ignore[method-assign]
        result = await tool.invoke(namespace="ns", chunk_ids=["aws:x"], profile={"userId": "u"})
        assert result.chunks == () and "entitlement" in result.detail

    @pytest.mark.asyncio
    async def test_graph_tool_withholds_statements_and_unentitled_entities(self):
        from coa_serve.tier3.agentic.tools.base import ToolResult
        from coa_serve.tier3.agentic.tools.graph_tool import GraphTraversalTool
        from coa_serve.tier3.graph_traverser import GraphEntity

        gate = MagicMock()

        async def _filter(chunks, namespace, user_id=None):
            return [c for c in chunks if c.uri == "e1"]

        async def _filter_entities(entities, namespace, user_id=None):
            assert user_id == "alice@example.com"
            return [e for e in entities if e.uri == "e1"]

        gate.filter_statement_chunks = _filter
        gate.filter_entities = _filter_entities
        tool = GraphTraversalTool("neptune-db://h:8182", entitlement_gate=gate)
        e1, e2 = (
            GraphEntity(uri="e1", label="E1", type="Thing"),
            GraphEntity(uri="e2", label="Project Falcon", type="T"),
        )
        inner = ToolResult(
            chunks=(
                ChunkResult("e1#stmt0", "ok", "", 1.0, uri="e1"),
                ChunkResult("e2#stmt0", "secret", "", 1.0, uri="e2"),
            ),
            entities=(e1, e2),
            items=(),
            detail="graph_traversal[edge_typed]: 2 connected nodes, 2 with text",
        )

        async def _inner(**kwargs):
            return inner

        tool._invoke = _inner  # type: ignore[method-assign]
        result = await tool.invoke(namespace="ns", mode="edge_typed", profile={"email": "alice@example.com"})
        assert [c.text for c in result.chunks] == ["ok"]
        assert result.entities == (e1,)
        assert "Project Falcon" not in repr(result)
        assert "1 statement(s) and 1 entity(ies) withheld" in result.detail

    @pytest.mark.asyncio
    async def test_graph_tool_gates_keyword_mode_entities_without_text(self):
        """Keyword mode returns labels only (no chunks); they are document-derived text and must be gated."""
        from coa_serve.tier3.agentic.tools.base import ToolResult
        from coa_serve.tier3.agentic.tools.graph_tool import GraphTraversalTool
        from coa_serve.tier3.graph_traverser import GraphEntity

        gate = MagicMock()

        async def _no_statements(chunks, namespace, user_id=None):
            return list(chunks)

        async def _only_e1(entities, namespace, user_id=None):
            return [e for e in entities if e.uri == "e1"]

        gate.filter_statement_chunks = _no_statements
        gate.filter_entities = _only_e1
        tool = GraphTraversalTool("neptune-db://h:8182", entitlement_gate=gate)
        inner = ToolResult(
            entities=(GraphEntity(uri="e1", label="Public", type="T"), GraphEntity(uri="e2", label="Secret", type="T")),
            detail="graph_traversal[keyword]: 2 entities",
        )

        async def _inner(**kwargs):
            return inner

        tool._invoke = _inner  # type: ignore[method-assign]
        result = await tool.invoke(namespace="ns", mode="keyword", query="x", profile={"userId": "alice@example.com"})
        assert [e.label for e in result.entities] == ["Public"]

    @pytest.mark.asyncio
    async def test_graph_tool_fails_closed_when_gate_errors(self):
        from coa_serve.tier3.agentic.tools.base import ToolResult
        from coa_serve.tier3.agentic.tools.graph_tool import GraphTraversalTool
        from coa_serve.tier3.graph_traverser import GraphEntity

        gate = MagicMock()

        async def _boom(*args, **kwargs):
            raise RuntimeError("neptune down")

        gate.filter_statement_chunks = _boom
        gate.filter_entities = _boom
        tool = GraphTraversalTool("neptune-db://h:8182", entitlement_gate=gate)
        inner = ToolResult(entities=(GraphEntity(uri="e2", label="Secret", type="T"),), detail="d")

        async def _inner(**kwargs):
            return inner

        tool._invoke = _inner  # type: ignore[method-assign]
        result = await tool.invoke(namespace="ns", mode="keyword", query="x", profile={"userId": "u"})
        assert result.entities == () and result.chunks == () and "entitlement" in result.detail

    @pytest.mark.asyncio
    async def test_tools_without_gate_are_unchanged(self):
        from coa_serve.tier3.agentic.tools.base import ToolResult
        from coa_serve.tier3.agentic.tools.graph_tool import GraphTraversalTool

        tool = GraphTraversalTool("neptune-db://h:8182")
        inner = ToolResult(chunks=(ChunkResult("e1#stmt0", "ok", "", 1.0, uri="e1"),), detail="d")

        async def _inner(**kwargs):
            return inner

        tool._invoke = _inner  # type: ignore[method-assign]
        assert await tool.invoke(namespace="ns") is inner


@pytest.mark.unit
class TestConfig:
    def test_defaults_and_knobs(self, monkeypatch):
        from coa_serve.config import load_config

        base = {
            "NEPTUNE_ENDPOINT": "h",
            "OPENSEARCH_ENDPOINT": "o",
            "DOCUMENT_RETRIEVER_BACKEND": "bedrock-kb",
            "BEDROCK_KNOWLEDGE_BASE_ID": KB,
        }
        for k, v in base.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("BEDROCK_KB_SEARCH_MODE", raising=False)
        monkeypatch.delenv("BEDROCK_KB_ENFORCE_USER_CONTEXT", raising=False)
        cfg = load_config()
        assert cfg.bedrock_kb_search_mode == "vector" and cfg.bedrock_kb_enforce_user_context is False
        # Managed KBs carry document ACL: per-user retrieval is implied unless opted out.
        monkeypatch.setenv("BEDROCK_KB_SEARCH_MODE", "managed")
        cfg = load_config()
        assert cfg.bedrock_kb_search_mode == "managed" and cfg.bedrock_kb_enforce_user_context is True
        monkeypatch.setenv("BEDROCK_KB_ENFORCE_USER_CONTEXT", "false")
        assert load_config().bedrock_kb_enforce_user_context is False  # explicit opt-out, logged
        monkeypatch.setenv("BEDROCK_KB_ENFORCE_USER_CONTEXT", "true")
        cfg = load_config()
        assert cfg.bedrock_kb_search_mode == "managed" and cfg.bedrock_kb_enforce_user_context is True
        from coa_serve.config import ConfigurationError

        monkeypatch.setenv("BEDROCK_KB_SEARCH_MODE", "hybrid")
        with pytest.raises(ConfigurationError):
            load_config()
