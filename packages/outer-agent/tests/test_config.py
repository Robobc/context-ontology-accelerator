import json
from urllib.parse import quote

import pytest
from coa_outer_agent.config import OuterAgentConfig

pytestmark = pytest.mark.unit


def _set_required(monkeypatch):
    values = {
        "COA_MCP_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        "SOURCE_TARGETS_JSON": ('{"domain-kb":{"kind":"MANAGED_KB","knowledgeBaseIds":["ABCDEFGHIJ"]}}'),
        "PORTKEY_MODEL_ID": "model",
        "PORTKEY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:1:secret:key",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_loads_and_encodes_runtime_configuration(monkeypatch):
    values = _set_required(monkeypatch)
    config = OuterAgentConfig.from_env()
    assert quote(values["COA_MCP_RUNTIME_ARN"], safe="") in config.coa_mcp_url
    assert config.source_targets["domain-kb"].knowledge_base_ids == ("ABCDEFGHIJ",)
    assert config.max_evidence_items == 12
    assert config.max_evidence_characters == 30000


def test_rejects_missing_required_value(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("PORTKEY_MODEL_ID")
    with pytest.raises(ValueError, match="PORTKEY_MODEL_ID is required"):
        OuterAgentConfig.from_env()


def test_rejects_invalid_limit(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("MAX_EVIDENCE_ITEMS", "0")
    with pytest.raises(ValueError, match="positive integer"):
        OuterAgentConfig.from_env()


def test_rejects_endpoint_or_credential_fields(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv(
        "SOURCE_TARGETS_JSON",
        '{"domain-kb":{"kind":"MANAGED_KB","knowledgeBaseIds":["ABCDEFGHIJ"],"token":"not-allowed"}}',
    )
    with pytest.raises(ValueError, match="unsupported fields"):
        OuterAgentConfig.from_env()


# ── ADR 0012: graph expansion knobs ──────────────────────────────────────────


def test_graph_expansion_defaults_to_seed_documents(monkeypatch):
    _set_required(monkeypatch)
    config = OuterAgentConfig.from_env()
    assert config.graph_expansion_scope == "SEED_DOCUMENTS"
    assert config.graph_expansion_max_facts == 100


@pytest.mark.parametrize(
    ("raw", "expected"), [("ENTITLED_DOCUMENTS", "ENTITLED_DOCUMENTS"), ("off", None), (" OFF ", None)]
)
def test_graph_expansion_scope_is_read_from_env(monkeypatch, raw, expected):
    _set_required(monkeypatch)
    monkeypatch.setenv("COA_GRAPH_EXPANSION_SCOPE", raw)
    assert OuterAgentConfig.from_env().graph_expansion_scope == expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COA_GRAPH_EXPANSION_SCOPE", "EVERYTHING"),
        ("COA_GRAPH_EXPANSION_MAX_FACTS", "0"),
        ("COA_GRAPH_EXPANSION_MAX_FACTS", "501"),
    ],
)
def test_graph_expansion_knobs_outside_the_contract_are_rejected(monkeypatch, name, value):
    _set_required(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        OuterAgentConfig.from_env()


def test_managed_kb_target_accepts_region_and_data_source_scope(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv(
        "SOURCE_TARGETS_JSON",
        '{"hub":{"kind":"MANAGED_KB","knowledgeBaseIds":["ABCDEFGHIJ"],'
        '"region":"eu-central-1","dataSourceIds":["DSTEST0001"]}}',
    )
    target = OuterAgentConfig.from_env().source_targets["hub"]
    assert target.region == "eu-central-1"
    assert target.data_source_ids == ("DSTEST0001",)


def test_managed_kb_target_defaults_to_runtime_region_and_whole_kb(monkeypatch):
    _set_required(monkeypatch)
    target = OuterAgentConfig.from_env().source_targets["domain-kb"]
    assert target.region is None
    assert target.data_source_ids == ()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("region", "eu-central-1; DROP", "region must be an AWS Region name"),
        ("region", "https://kb.example", "region must be an AWS Region name"),
        ("dataSourceIds", ["not valid"], "dataSourceIds must be 0-10"),
        ("dataSourceIds", ["DSTEST0001"] * 11, "dataSourceIds must be 0-10"),
        ("dataSourceIds", "DSTEST0001", "dataSourceIds must be 0-10"),
    ],
)
def test_managed_kb_scope_fields_outside_the_grammar_are_rejected(monkeypatch, field, value, message):
    _set_required(monkeypatch)
    item = {"kind": "MANAGED_KB", "knowledgeBaseIds": ["ABCDEFGHIJ"], field: value}
    monkeypatch.setenv("SOURCE_TARGETS_JSON", json.dumps({"hub": item}))
    with pytest.raises(ValueError, match=message):
        OuterAgentConfig.from_env()


def test_non_kb_target_rejects_region_and_data_source_scope(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("SOURCE_TARGETS_JSON", '{"graph":{"kind":"COA_GRAPH","region":"eu-central-1"}}')
    with pytest.raises(ValueError, match="unsupported fields"):
        OuterAgentConfig.from_env()
