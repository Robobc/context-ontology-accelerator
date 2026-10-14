"""Small authenticated MCP client used for COA and Gateway tools."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class McpToolError(RuntimeError):
    """Raised when a downstream MCP tool reports an error."""


class McpJsonClient:
    """Authenticated JSON-oriented adapter over MCP Streamable HTTP."""

    def __init__(
        self,
        url: str,
        bearer_token: str,
        timeout_seconds: int = 120,
    ):
        """Configure the endpoint, caller token, and request timeout."""
        if not bearer_token:
            raise ValueError("MCP bearer token is required")
        self._url = url
        self._token = bearer_token
        self._timeout = timeout_seconds

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Initialize an MCP session and return normalized tool content."""
        timeout = httpx.Timeout(self._timeout, connect=10)
        headers = {"Authorization": f"Bearer {self._token}"} if self._token is not None else {}
        async with (
            httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            ) as http_client,
            streamable_http_client(
                self._url,
                http_client=http_client,
                terminate_on_close=False,
            ) as (read_stream, write_stream, _),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self._timeout),
            ) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                name,
                arguments,
                read_timeout_seconds=timedelta(seconds=self._timeout),
            )

        if result.isError:
            raise McpToolError(self._text_error(result.content))
        if result.structuredContent is not None:
            return result.structuredContent

        values: list[Any] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if not isinstance(text, str):
                continue
            try:
                values.append(json.loads(text))
            except json.JSONDecodeError:
                values.append(text)
        if not values:
            return {}
        return values[0] if len(values) == 1 else values

    @staticmethod
    def _text_error(content: list[Any]) -> str:
        parts = [text for item in content if isinstance((text := getattr(item, "text", None)), str)]
        return " ".join(parts) or "Downstream MCP tool failed"
