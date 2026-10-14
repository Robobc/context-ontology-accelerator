# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Portkey generation gateway with direct Bedrock embeddings and guardrails."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

import boto3
import httpx
import structlog
from botocore.config import Config as BotoConfig
from coa_common import (
    delimit_model_input,
    resolve_region,
    secure_system_prompt,
    strip_hidden_model_characters,
    validate_model_output_text,
)

from .base import (
    STOP_REASON_MAX_TOKENS,
    ContentFilteredError,
    ConverseResult,
    GuardrailBlockedError,
    GuardrailOutcome,
    LLMClient,
    instrumented,
)
from .guardrails import ApplyGuardrailPolicy

logger = structlog.get_logger(__name__)


def _extract_text_content(content: Any) -> str:
    """Normalize text from OpenAI-compatible string or content-part responses."""
    if isinstance(content, str):
        return content

    parts = content if isinstance(content, list) else [content]
    text_parts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            text_parts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    return "".join(text_parts)


def _unsupported_sampling_parameters(response: httpx.Response) -> set[str]:
    """Find sampling parameters a gateway reports as unsupported."""
    if response.status_code not in (400, 422):
        return set()
    try:
        detail = json.dumps(response.json(), default=str).casefold()
    except (TypeError, ValueError):
        detail = response.text.casefold()
    if not any(marker in detail for marker in ("deprecated", "unsupported", "not supported")):
        return set()
    return {name for name in ("temperature", "top_p") if name in detail}


class PortkeyLLMClient:
    """LLM client that sends generation to Portkey and embeddings to Bedrock."""

    def __init__(
        self,
        *,
        model_id: str,
        embedding_client: LLMClient,
        base_url: str = "https://api.portkey.ai/v1",
        api_key: str | None = None,
        secret_arn: str | None = None,
        virtual_key: str | None = None,
        config_id: str | None = None,
        region: str | None = None,
        guardrail_version: str = "DRAFT",
        http_client: httpx.AsyncClient | None = None,
        secrets_client: Any | None = None,
        guardrail_policy: ApplyGuardrailPolicy | None = None,
    ) -> None:
        """Configure Portkey, credential loading, embeddings, and safety."""
        if not model_id:
            raise ValueError("Portkey model_id is required")
        if not api_key and not secret_arn:
            raise ValueError("Portkey api_key or secret_arn is required")
        self._model_id = model_id
        self._embedding_client = embedding_client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._secret_arn = secret_arn
        self._virtual_key = virtual_key
        self._config_id = config_id
        self._region = region or resolve_region()
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._secrets_client = secrets_client
        self._secrets_lock = threading.Lock()
        self._guardrail = guardrail_policy or ApplyGuardrailPolicy(
            region=self._region,
            guardrail_version=guardrail_version,
        )

    @property
    def model_id(self) -> str:
        """Return the model identifier configured in Portkey."""
        return self._model_id

    @property
    def provider_name(self) -> str:
        """Return the stable trace label for gateway model generation."""
        return "portkey"

    def _get_secrets_client(self):
        if self._secrets_client is None:
            with self._secrets_lock:
                if self._secrets_client is None:
                    self._secrets_client = boto3.client(
                        "secretsmanager",
                        region_name=self._region,
                        config=BotoConfig(
                            read_timeout=10,
                            connect_timeout=5,
                            retries={"max_attempts": 2},
                        ),
                    )
        return self._secrets_client

    async def _ensure_credentials(self) -> None:
        if self._api_key:
            return
        response = await asyncio.to_thread(
            self._get_secrets_client().get_secret_value,
            SecretId=self._secret_arn,
        )
        secret = response.get("SecretString", "")
        try:
            values = json.loads(secret)
        except (json.JSONDecodeError, TypeError):
            values = {"apiKey": secret}
        if not isinstance(values, dict) or not values.get("apiKey"):
            raise ValueError("Portkey secret must contain apiKey")
        self._api_key = str(values["apiKey"])
        self._virtual_key = self._virtual_key or values.get("virtualKey")
        self._config_id = self._config_id or values.get("configId")

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(
                    float(os.environ.get("PORTKEY_READ_TIMEOUT", "120")),
                    connect=float(os.environ.get("PORTKEY_CONNECT_TIMEOUT", "10")),
                ),
            )
        return self._http_client

    async def _screen(
        self,
        text: str,
        *,
        guardrail_id: str | None,
        source: Literal["INPUT", "OUTPUT"],
    ):
        if not guardrail_id:
            return text, GuardrailOutcome.NONE
        result = await self._guardrail.apply(
            text,
            guardrail_id=guardrail_id,
            source=source,
        )
        return result.text, result.outcome

    @instrumented("portkey")
    async def converse(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        top_p: float | None = None,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> ConverseResult:
        """Generate via Portkey with independent input and output screening."""
        raw_prompt = strip_hidden_model_characters(prompt)
        raw_guard_content = strip_hidden_model_characters(guard_content) if guard_content is not None else None
        system = secure_system_prompt(system)
        # @amsec-loaf: model-validation
        # Guardrails evaluate only sanitized untrusted content. Model-only
        # non-disclosure instructions are added after screening; evaluating our
        # own "Never reveal" text as user input triggers PROMPT_ATTACK.
        safe_guard_content, input_outcome = await self._screen(
            raw_guard_content if raw_guard_content is not None else raw_prompt,
            guardrail_id=guardrail_id,
            source="INPUT",
        )
        if input_outcome in (GuardrailOutcome.BLOCKED, GuardrailOutcome.UNKNOWN):
            return ConverseResult(
                text=safe_guard_content,
                outcome=input_outcome,
                stop_reason="guardrail_intervened",
            )

        prompt = delimit_model_input(raw_prompt)
        user_content = prompt
        if raw_guard_content is not None:
            user_content = f"{prompt}\n\n{delimit_model_input(safe_guard_content)}"
        elif input_outcome is GuardrailOutcome.ANONYMIZED:
            user_content = delimit_model_input(safe_guard_content)

        await self._ensure_credentials()
        headers: dict[str, str] = {
            "x-portkey-api-key": self._api_key or "",
            "x-portkey-trace-id": str(uuid.uuid4()),
        }
        if self._virtual_key:
            headers["x-portkey-virtual-key"] = self._virtual_key
        if self._config_id:
            headers["x-portkey-config"] = self._config_id

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
        payload: dict[str, Any] = {
            "model": model_id or self._model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p

        for attempt in range(3):
            response = await self._get_http_client().post(
                "/chat/completions",
                headers=headers,
                json=payload,
            )
            try:
                response.raise_for_status()
                break
            except httpx.HTTPStatusError:
                unsupported = _unsupported_sampling_parameters(response).intersection(payload)
                if unsupported and attempt < 2:
                    for parameter in unsupported:
                        payload.pop(parameter)
                    logger.info(
                        "portkey_sampling_parameter_removed",
                        parameters=sorted(unsupported),
                        model=payload["model"],
                    )
                    continue
                logger.error(
                    "portkey_request_failed",
                    status_code=response.status_code,
                    model=payload["model"],
                )
                raise

        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Portkey response was not a JSON object")
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ValueError("Portkey response contained no choices")
        choice = choices[0]
        finish_reason = choice.get("finish_reason") or ""
        message = choice.get("message") or {}
        refusal = message.get("refusal") if isinstance(message, dict) else None
        if finish_reason == "content_filter" or (isinstance(refusal, str) and refusal):
            raise ContentFilteredError(
                "Portkey model filtered its output",
                stop_reason=str(finish_reason or "content_filtered"),
            )
        content = message.get("content") if isinstance(message, dict) else None
        text = validate_model_output_text(_extract_text_content(content))
        if not text:
            raise ValueError(
                "Portkey response contained no text: "
                f"finish_reason={finish_reason or 'unknown'}, "
                f"content_type={type(content).__name__}"
            )

        stop_reason = STOP_REASON_MAX_TOKENS if finish_reason == "length" else str(finish_reason)

        safe_output, output_outcome = await self._screen(
            text,
            guardrail_id=guardrail_id,
            source="OUTPUT",
        )
        return ConverseResult(
            text=safe_output,
            outcome=output_outcome,
            stop_reason=(
                "guardrail_intervened"
                if output_outcome in (GuardrailOutcome.BLOCKED, GuardrailOutcome.UNKNOWN)
                else stop_reason
            ),
        )

    async def converse_stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Buffer generation until output screening completes, then emit it."""
        result = await self.converse(
            prompt,
            system=system,
            guardrail_id=guardrail_id,
            max_tokens=max_tokens,
            guard_content=guard_content,
            model_id=model_id,
        )
        if result.guardrail_blocked:
            raise GuardrailBlockedError(f"guardrail_{result.outcome.value}")
        if result.text:
            yield result.text

    async def embed(self, text: str) -> list[float]:
        """Generate embeddings directly through the existing Bedrock client."""
        return await self._embedding_client.embed(text)

    async def health_check(self) -> dict[str, Any]:
        """Verify credentials are readable without issuing a billable generation."""
        try:
            await self._ensure_credentials()
            return {"status": "ok", "model": self._model_id, "gateway": "portkey"}
        except Exception:
            return {"status": "error", "detail": "Portkey credentials unavailable"}

    async def close(self) -> None:
        """Release HTTP, safety, secret, and embedding clients."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
        close_guardrail = getattr(self._guardrail, "close", None)
        if callable(close_guardrail):
            await close_guardrail()
        close_embedding = getattr(self._embedding_client, "close", None)
        if callable(close_embedding):
            await close_embedding()
        if self._secrets_client is not None:
            close_secrets = getattr(self._secrets_client, "close", None)
            if callable(close_secrets):
                close_secrets()
