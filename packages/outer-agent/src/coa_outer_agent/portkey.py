"""Final generation through Portkey with independent Bedrock Guardrails."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import boto3
import httpx
from coa_common import (
    delimit_model_input,
    secure_system_prompt,
    strip_hidden_model_characters,
    validate_model_output_text,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class GuardrailRejected(ValueError):
    """Raised when the configured guardrail intervenes."""


_EVIDENCE_ID = re.compile(r"\bE[1-9][0-9]*\b")


class _Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    evidenceIds: list[str] = Field(min_length=1, max_length=8)

    @field_validator("evidenceIds")
    @classmethod
    def valid_evidence_ids(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"E[1-9][0-9]*", item) for item in value):
            raise ValueError("invalid evidence identifier")
        return list(dict.fromkeys(value))


class _GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[_Claim] = Field(min_length=1, max_length=50)


class PortkeyGenerationClient:
    """Final-answer client using the existing Portkey and guardrail settings."""

    def __init__(
        self,
        *,
        region: str,
        base_url: str,
        model_id: str,
        secret_arn: str,
        guardrail_id: str,
        guardrail_version: str,
        http_client: httpx.AsyncClient | None = None,
        secrets_client: Any | None = None,
        bedrock_client: Any | None = None,
    ):
        """Configure Portkey, secret loading, and independent guardrails."""
        self._region = region
        self._base_url = base_url
        self._model_id = model_id
        self._secret_arn = secret_arn
        self._guardrail_id = "" if guardrail_id.lower() == "none" else guardrail_id
        self._guardrail_version = guardrail_version
        self._http_client = http_client
        self._secrets = secrets_client
        self._bedrock = bedrock_client
        self._credentials: dict[str, str] | None = None

    async def screen(self, text: str, source: str) -> None:
        """Reject content when the configured Bedrock Guardrail intervenes."""
        if not self._guardrail_id:
            return
        response = await asyncio.to_thread(
            self._bedrock_client().apply_guardrail,
            guardrailIdentifier=self._guardrail_id,
            guardrailVersion=self._guardrail_version,
            source=source,
            content=[{"text": {"text": text}}],
        )
        if response.get("action") == "GUARDRAIL_INTERVENED":
            raise GuardrailRejected(f"Bedrock Guardrail rejected {source.lower()} content")

    async def generate(
        self,
        *,
        question: str,
        semantic_plan: dict[str, Any],
        context: str,
    ) -> str:
        """Generate one cited answer from an authorized evidence context."""
        credentials = await self._load_credentials()
        headers = {
            "x-portkey-api-key": credentials["apiKey"],
        }
        if virtual_key := credentials.get("virtualKey"):
            headers["x-portkey-virtual-key"] = virtual_key
        if config_id := credentials.get("configId"):
            headers["x-portkey-config"] = config_id

        system = secure_system_prompt(
            "Return JSON only with shape "
            '{"claims":[{"text":"one independently supportable claim",'
            '"evidenceIds":["E1"]}]}. Every claim must cite one or more evidence '
            "IDs present in the authorized evidence. Do not use the semantic plan "
            "as evidence and do not add an overall uncited summary."
        )
        raw_prompt = strip_hidden_model_characters(
            f"Question:\n{question}\n\n"
            f"Semantic plan:\n{json.dumps(semantic_plan, default=str)}\n\n"
            f"Authorized evidence:\n{context}"
        )
        # @amsec-loaf: model-validation
        # Screen only sanitized untrusted content. Adding our model-only
        # non-disclosure wrapper first makes PROMPT_ATTACK classify the wrapper
        # rather than the caller's question and evidence.
        await self.screen(raw_prompt, "INPUT")
        prompt = delimit_model_input(raw_prompt)
        response = await self._client().post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json={
                "model": self._model_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 2048,
                "stream": False,
            },
        )
        response.raise_for_status()
        body = response.json()
        try:
            raw_answer = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Portkey response contained no answer") from exc
        if not isinstance(raw_answer, str) or not raw_answer.strip():
            raise ValueError("Portkey response contained no answer")

        raw_answer = validate_model_output_text(raw_answer).strip()
        if raw_answer.startswith("```"):
            raw_answer = raw_answer.split("\n", 1)[1] if "\n" in raw_answer else raw_answer[3:]
            if raw_answer.endswith("```"):
                raw_answer = raw_answer[:-3]
            raw_answer = raw_answer.strip()
        try:
            grounded = _GroundedAnswer.model_validate_json(raw_answer)
        except ValidationError as exc:
            raise ValueError("Portkey response failed the grounded-answer contract") from exc

        available_ids = set(_EVIDENCE_ID.findall(context))
        for claim in grounded.claims:
            if not set(claim.evidenceIds).issubset(available_ids):
                raise ValueError("Portkey response cited unknown evidence")
        answer = "\n".join(f"{claim.text} [{', '.join(claim.evidenceIds)}]" for claim in grounded.claims)
        await self.screen(answer, "OUTPUT")
        return answer

    async def _load_credentials(self) -> dict[str, str]:
        if self._credentials is not None:
            return self._credentials
        response = await asyncio.to_thread(
            self._secrets_client().get_secret_value,
            SecretId=self._secret_arn,
        )
        raw = response.get("SecretString", "")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            value = {"apiKey": raw}
        if not isinstance(value, dict) or not value.get("apiKey"):
            raise ValueError("Portkey secret must contain apiKey")
        self._credentials = {key: str(value[key]) for key in ("apiKey", "virtualKey", "configId") if value.get(key)}
        return self._credentials

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10))
        return self._http_client

    def _secrets_client(self):
        if self._secrets is None:
            self._secrets = boto3.client("secretsmanager", region_name=self._region)
        return self._secrets

    def _bedrock_client(self):
        if self._bedrock is None:
            self._bedrock = boto3.client("bedrock-runtime", region_name=self._region)
        return self._bedrock
