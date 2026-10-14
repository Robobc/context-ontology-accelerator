from types import SimpleNamespace

import pytest
from coa_outer_agent.mcp_client import McpJsonClient

pytestmark = pytest.mark.unit


def test_text_error_combines_text_content():
    content = [
        SimpleNamespace(text="first"),
        SimpleNamespace(text="second"),
        SimpleNamespace(),
    ]
    assert McpJsonClient._text_error(content) == "first second"


def test_text_error_has_safe_fallback():
    assert McpJsonClient._text_error([]) == "Downstream MCP tool failed"


def test_mcp_client_requires_bearer_authentication():
    with pytest.raises(ValueError, match="bearer token"):
        McpJsonClient("https://example.com", "")


def test_mcp_client_keeps_validated_bearer_token():
    client = McpJsonClient("https://example.com", "validated-token")
    assert client._token == "validated-token"
