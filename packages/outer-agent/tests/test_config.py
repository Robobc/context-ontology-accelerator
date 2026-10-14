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
