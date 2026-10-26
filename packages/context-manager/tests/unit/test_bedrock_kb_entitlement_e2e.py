# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fork: document entitlement end to end, through the real wiring.

The unit tests in ``test_bedrock_kb_entitlement.py`` mock the gate; they would
still pass if the gate were unwired from the live path. These tests build the
gate the way ``main.py`` does (``build_document_entitlement_gate``), hand it to
the real ``build_tool_registry`` / ``build_lexical_retriever``, replace only the
Neptune round-trip and the toolkit retrieval with one fake lexical graph, and
assert what reaches the caller. Each ACL assertion has a control: with the gate
left out (today's default for non-KB namespaces) the restricted text IS returned,
so the assertion is sensitive to the wiring, not just to the fake.

Fake lexical graph (one namespace, three documents under the KB's ACL):

    finance doc  -> chunk aws:fin1 -> statement "Q3 revenue grew 8%."  -> entity e_rev "Q3 Revenue"
    hr doc       -> chunk aws:hr1  -> statement "Band 4 pays 120k."    -> entity e_band "Band 4"
    shared doc   -> chunk aws:sh1  -> statement "Handbook v3."         -> entity e_hb "Handbook"

alice may read finance + shared; carol nothing (see ``AclKbClient``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from coa_serve.request_identity import get_request_user, reset_request_user, set_request_user
from coa_serve.tier3.bedrock_kb_retriever import BedrockKnowledgeBaseRetriever
from coa_serve.tier3.document_entitlement import build_document_entitlement_gate
from coa_serve.tier3.document_retriever import RoutingDocumentRetriever

from .test_bedrock_kb_entitlement import FINANCE, HR, KB, SHARED, AclKbClient
from .test_tier3_agentic_factory import make_clients, make_config

NS = "0f1e2d3c-4b5a-4697-8899-aabbccddeeff"  # tenant labels derive from a UUID namespace id
GRAPH_URI = "neptune-db://h:8182"

CHUNK_OF = {"aws:fin1": FINANCE, "aws:hr1": HR, "aws:sh1": SHARED}
SOURCE_OF = {"aws:doc:fin": FINANCE, "aws:doc:hr": HR, "aws:doc:sh": SHARED}
ENTITIES = {
    "e_rev": ("Q3 Revenue", "Q3 revenue grew 8%.", FINANCE),
    "e_band": ("Band 4", "Band 4 pays 120k.", HR),
    "e_hb": ("Handbook", "Handbook v3.", SHARED),
}


async def fake_neptune(cypher: str, params: dict) -> list[dict]:
    """Answer the four provenance templates and the graph tool's keyword/edge templates."""
    if "RETURN id(c) AS chunkId, s.source_uri AS uri" in cypher:
        return [{"chunkId": c, "uri": CHUNK_OF[c]} for c in params["chunk_ids"] if c in CHUNK_OF]
    if "RETURN DISTINCT id(e) AS entityId" in cypher:
        return [{"entityId": e, "uri": ENTITIES[e][2]} for e in params["entity_ids"] if e in ENTITIES]
    if "RETURN id(e) AS entityId, st.value AS text" in cypher:
        return [{"entityId": e, "text": ENTITIES[e][1], "uri": ENTITIES[e][2]} for e in params["entity_ids"]]
    if "RETURN id(s) AS sourceId" in cypher:
        return [{"sourceId": s, "uri": SOURCE_OF[s]} for s in params["source_ids"] if s in SOURCE_OF]
    if "$probes" in cypher:  # keyword entity search: every entity matches
        return [{"uri": e, "label": label, "class": "T"} for e, (label, _, _) in ENTITIES.items()]
    if "$entity_ids" in cypher:  # edge-typed traversal: each entity reached as a target with its statement
        return [
            {
                "source": "e_seed",
                "edge": "related_to",
                "target": e,
                "targetLabel": ENTITIES[e][0],
                "targetClass": "T",
                "targetTexts": [ENTITIES[e][1]],
            }
            for e in params["entity_ids"]
        ]
    raise AssertionError(f"unexpected cypher: {cypher[:80]}")


def _document_retriever():
    per_user = BedrockKnowledgeBaseRetriever(KB, client=AclKbClient(), search_mode="managed", enforce_user_context=True)
    return RoutingDocumentRetriever(default=SimpleNamespace(), routes={NS: per_user})


def _registry(gate):
    from coa_serve.tier3.agentic.factory import build_tool_registry

    config = make_config(neptune_endpoint="h:8182", opensearch_endpoint="o:443")
    with patch("coa_serve.tier3.agentic.tools.graph_tool.GraphTraversalTool._run_cypher", staticmethod(fake_neptune)):
        return build_tool_registry(config, make_clients(), entitlement_gate=gate)


def _wire(gate):
    """Point the gate's Neptune round-trip at the fake graph; the gate itself is real."""
    gate._run_cypher = fake_neptune  # type: ignore[method-assign]
    gate._label = staticmethod(lambda ns, name: f"`{name}`")  # type: ignore[method-assign]
    return gate


@pytest.fixture()
def gate():
    g = build_document_entitlement_gate(GRAPH_URI, _document_retriever())
    assert g is not None, "a per-user KB route must produce a gate"
    return _wire(g)


@pytest.fixture()
def as_alice():
    token = set_request_user("alice@example.com")
    yield
    reset_request_user(token)


@pytest.mark.unit
class TestGraphToolThroughTheRegistry:
    @pytest.mark.asyncio
    async def test_keyword_mode_hides_entities_from_documents_alice_cannot_read(self, gate, as_alice):
        tool = _registry(gate).get("graph_traversal")
        with patch.object(type(tool), "_run_cypher", staticmethod(fake_neptune)):
            result = await tool.invoke(namespace=NS, mode="keyword", query="revenue handbook band")
        assert sorted(e.label for e in result.entities) == ["Handbook", "Q3 Revenue"]
        assert "Band 4" not in repr(result)

    @pytest.mark.asyncio
    async def test_edge_typed_mode_hides_hr_statement_and_entity(self, gate, as_alice):
        tool = _registry(gate).get("graph_traversal")
        with patch.object(type(tool), "_run_cypher", staticmethod(fake_neptune)):
            result = await tool.invoke(
                namespace=NS, mode="edge_typed", entity_ids=list(ENTITIES), edge_types=["related_to"], limit=10
            )
        assert "Band 4" not in repr(result) and "120k" not in repr(result)
        assert "Q3 revenue grew 8%." in [c.text for c in result.chunks]

    @pytest.mark.asyncio
    async def test_carol_gets_nothing_and_no_caller_gets_nothing(self, gate):
        tool = _registry(gate).get("graph_traversal")
        with patch.object(type(tool), "_run_cypher", staticmethod(fake_neptune)):
            assert get_request_user() == ""
            anonymous = await tool.invoke(namespace=NS, mode="keyword", query="revenue")
            carol = await tool.invoke(
                namespace=NS, mode="keyword", query="revenue", profile={"email": "carol@example.com"}
            )
        assert anonymous.entities == () and carol.entities == ()

    @pytest.mark.asyncio
    async def test_control_without_gate_returns_everything(self, as_alice):
        """Sensitivity check: the same fake graph leaks HR when the gate is not wired."""
        tool = _registry(None).get("graph_traversal")
        with patch.object(type(tool), "_run_cypher", staticmethod(fake_neptune)):
            result = await tool.invoke(namespace=NS, mode="keyword", query="revenue")
        assert "Band 4" in [e.label for e in result.entities]


@pytest.mark.unit
class TestLexicalBaselineThroughTheFactory:
    def _nodes(self):
        def node(text, source):
            return SimpleNamespace(node_id=text, text=text, score=0.5, metadata={"source": {"sourceId": source}})

        return [node("Q3 revenue grew 8%.", "aws:doc:fin"), node("Band 4 pays 120k.", "aws:doc:hr")]

    def _retriever(self, gate):
        from coa_serve.clients.factory import build_lexical_retriever

        config = make_config(neptune_endpoint="h:8182", opensearch_endpoint="o:443")
        retriever = build_lexical_retriever(config, entitlement_gate=gate)
        assert retriever is not None
        retriever._retrieve_sync = lambda query, namespace, strategy: self._nodes()  # type: ignore[method-assign]
        return retriever

    @pytest.mark.asyncio
    async def test_baseline_path_drops_hr_text_for_alice(self, gate, as_alice):
        result = await self._retriever(gate).retrieve("pay bands", NS)
        assert [c.text for c in result.chunks] == ["Q3 revenue grew 8%."]

    @pytest.mark.asyncio
    async def test_baseline_path_control_without_gate(self, as_alice):
        result = await self._retriever(None).retrieve("pay bands", NS)
        assert "Band 4 pays 120k." in [c.text for c in result.chunks]

    @pytest.mark.asyncio
    async def test_strategy_tools_share_the_same_gated_adapter(self, gate, as_alice):
        """The agentic strategy:* tools wrap the same adapter, so they are gated by construction."""
        registry = _registry(gate)
        tool = registry.get(next(name for name in registry.names() if name.startswith("strategy:")))
        tool._lexical._retrieve_sync = lambda query, namespace, strategy: self._nodes()  # type: ignore[method-assign]
        result = await tool.invoke(namespace=NS, query="pay bands")
        assert "120k" not in repr(result) and "Q3 revenue grew 8%." in repr(result)


@pytest.mark.unit
class TestEntrypointIdentityScope:
    """The caller published during one request never survives into the next (worker reuse)."""

    @pytest.mark.asyncio
    async def test_identity_is_cleared_on_entry_and_reset_on_exit(self):
        import coa_serve.main as main_mod

        seen: list[str] = []

        async def fake_body(payload, context):
            seen.append(get_request_user())  # what a request that skips admission would read
            set_request_user("alice@example.com")  # what the admission gate does
            yield {"ok": True}

        leaked = set_request_user("previous-caller@example.com")  # a stale identity on this worker
        try:
            with patch.object(main_mod, "_invoke", fake_body):
                events = [e async for e in main_mod.invoke({"action": "listSessions"}, None)]
            assert events == [{"ok": True}]
            assert seen == [""]  # cleared on entry: the stale caller was not visible to the body
            assert get_request_user() == "previous-caller@example.com"  # reset to the outer value on exit
        finally:
            reset_request_user(leaked)

    @pytest.mark.asyncio
    async def test_reset_also_runs_when_the_body_raises(self):
        import coa_serve.main as main_mod

        async def failing_body(payload, context):
            set_request_user("alice@example.com")
            raise RuntimeError("boom")
            yield  # pragma: no cover - makes this an async generator

        with patch.object(main_mod, "_invoke", failing_body), pytest.raises(RuntimeError):
            async for _ in main_mod.invoke({}, None):
                pass
        assert get_request_user() == ""
