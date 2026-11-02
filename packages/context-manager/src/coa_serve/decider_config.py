# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Live (TTL-refreshed) provider for the deep-reasoning Decider settings (ADR 0014).

The Decider's backend and model are the one piece of deep-reasoning
configuration an operator wants to change *without a redeploy*: switch it off,
switch it on, point it at a cheaper model, or at a Jev-class classifier routed
through Portkey, and compare on the same running image. ``ServiceConfig`` is
frozen at process start, so, like the guardrail ids (``guardrail_config.py``),
these settings are read from one SSM parameter on a short TTL:

    <SSM_PREFIX>/deep-reasoning/decider   (String, JSON)
    {"backend": "llm", "modelId": "amazon.nova-micro-v1:0",
     "minConfidence": 0.7, "timeoutS": 3, "maxStateChars": 6000}

Every key is optional. A key that is absent falls back to the environment
default (``DECIDER_BACKEND``, ``DECIDER_MODEL_ID``, ``DECIDER_MIN_CONFIDENCE``,
``DECIDER_TIMEOUT_S``), and an absent parameter means "environment defaults
only", which is how a deployment that never creates the parameter behaves
exactly as the CDK context says. A parameter that is not valid JSON, or names
an unknown backend, is logged and ignored: last-known-good when there is one,
the environment defaults otherwise. The provider never raises on the hot path.

Same mechanics as :class:`GuardrailConfigProvider`: stale-while-revalidate on
a 60 s TTL, one ``GetParameter`` per instance per TTL, an INFO line only when
the effective settings change (the signal that answers "did my edit take?").
"""

from __future__ import annotations

import json
import os
import threading
import time

import structlog

from .config import ServiceConfig, _read_ssm_parameter_strict
from .tier3.agentic.decider import DECIDER_BACKENDS, DeciderSettings

logger = structlog.get_logger(__name__)

DEFAULT_DECIDER_TTL_S = 60.0
DECIDER_PARAMETER_SUFFIX = "/deep-reasoning/decider"


def settings_from_config(config: ServiceConfig) -> DeciderSettings:
    """The environment-backed defaults, as :class:`DeciderSettings`."""
    return DeciderSettings(
        backend=config.decider_backend,
        model_id=config.decider_model_id,
        min_confidence=config.decider_min_confidence,
        timeout_s=config.decider_timeout_s,
    ).normalised()


def settings_from_json(raw: str, defaults: DeciderSettings) -> DeciderSettings:
    """Overlay the SSM JSON document on ``defaults``; raises ``ValueError`` when unusable.

    Unknown keys are ignored. An unknown ``backend`` is an error (not a silent
    ``off``) so the operator sees the typo in the log rather than a decider that
    looks configured and never fires.
    """
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("decider parameter must be a JSON object")
    backend = str(doc.get("backend", defaults.backend)).strip().lower()
    if backend not in DECIDER_BACKENDS:
        raise ValueError(f"unknown decider backend {backend!r}; expected one of {sorted(DECIDER_BACKENDS)}")
    return DeciderSettings(
        backend=backend,
        model_id=str(doc.get("modelId", defaults.model_id) or ""),
        min_confidence=float(doc.get("minConfidence", defaults.min_confidence)),
        timeout_s=float(doc.get("timeoutS", defaults.timeout_s)),
        max_state_chars=int(doc.get("maxStateChars", defaults.max_state_chars)),
    ).normalised()


class DeciderConfigProvider:
    """Live, TTL-refreshed :class:`DeciderSettings` from one SSM parameter over env defaults."""

    def __init__(
        self,
        ssm_prefix: str,
        defaults: DeciderSettings,
        *,
        ttl_s: float = DEFAULT_DECIDER_TTL_S,
        time_fn=time.monotonic,
        ssm_getter=_read_ssm_parameter_strict,
    ) -> None:
        """Bind the parameter path, the environment defaults and the injectable clock/reader."""
        self._param = ssm_prefix.rstrip("/") + DECIDER_PARAMETER_SUFFIX
        self._defaults = defaults
        self._ttl_s = ttl_s
        self._time_fn = time_fn
        self._ssm_getter = ssm_getter
        self._lock = threading.Lock()
        self._snapshot: DeciderSettings | None = None
        self._loaded_at = 0.0

    @property
    def parameter_name(self) -> str:
        """The SSM parameter this provider reads."""
        return self._param

    def _read(self) -> DeciderSettings:
        raw = self._ssm_getter(self._param, required=False)
        if not raw:
            return self._defaults
        return settings_from_json(raw, self._defaults)

    def settings(self) -> DeciderSettings:
        """Current settings; refreshes past the TTL, last-known-good on any failure."""
        now = self._time_fn()
        snapshot = self._snapshot
        if snapshot is not None and (now - self._loaded_at) < self._ttl_s:
            return snapshot
        with self._lock:
            now = self._time_fn()
            if self._snapshot is not None and (now - self._loaded_at) < self._ttl_s:
                return self._snapshot
            prev = self._snapshot
            try:
                fresh = self._read()
            except Exception as exc:  # noqa: BLE001 - last-known-good is the whole point
                logger.warning(
                    "decider_config_refresh_failed",
                    parameter=self._param,
                    error=type(exc).__name__,
                    error_msg=str(exc)[:200],
                    serving="cached" if prev is not None else "env_defaults",
                )
                if prev is not None:
                    return prev  # _loaded_at not advanced: retry on the next call
                fresh = self._defaults
            self._snapshot = fresh
            self._loaded_at = now
            if prev is None:
                logger.info("decider_config_loaded", parameter=self._param, **_fields(fresh))
            elif prev != fresh:
                logger.info("decider_config_changed", parameter=self._param, **_fields(fresh), ttl_s=self._ttl_s)
            return fresh

    def prime(self) -> None:
        """Eagerly perform the first read (the accessor does it lazily otherwise)."""
        self.settings()


def _fields(s: DeciderSettings) -> dict:
    return {
        "backend": s.backend,
        "model_id": s.model_id or "default",
        "min_confidence": s.min_confidence,
        "timeout_s": s.timeout_s,
    }


def build_decider_config_provider(config: ServiceConfig, ttl_s: float = DEFAULT_DECIDER_TTL_S) -> DeciderConfigProvider:
    """Construct a provider using the same ``SSM_PREFIX`` resolution as ``load_config``."""
    return DeciderConfigProvider(os.environ.get("SSM_PREFIX", "/coa"), settings_from_config(config), ttl_s=ttl_s)
