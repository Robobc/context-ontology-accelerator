# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Validated namespace integration profiles for source-native execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROFILE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROFILE_MODES = frozenset({"ACCELERATE", "ADOPT", "FEDERATE"})
_TARGET_KINDS = frozenset({"MANAGED_KB", "GOVERNED_QUERY", "EXTERNAL_GRAPH", "COA_GRAPH"})
_MAX_SOURCE_TARGETS = 8

_DEFAULT_PROFILE: dict[str, Any] = {
    "mode": "ACCELERATE",
    "profileVersion": "1",
    "ontologyVersion": "legacy-unversioned",
    "sourceTargets": [
        {
            "targetId": "coa-graph",
            "kind": "COA_GRAPH",
            "accessMode": "READ_ONLY",
        }
    ],
    "generationTarget": "bedrock",
}


class IntegrationProfileError(ValueError):
    """Raised when a namespace integration profile is unsafe or malformed."""


def default_namespace_integration_profile() -> dict[str, Any]:
    """Return a fresh copy of the backwards-compatible Accelerate profile."""
    return {
        **_DEFAULT_PROFILE,
        "sourceTargets": [dict(target) for target in _DEFAULT_PROFILE["sourceTargets"]],
    }


def normalize_namespace_integration_profile(value: Any | None) -> dict[str, Any]:
    """Return a canonical profile containing only allowlisted fields.

    Profiles contain logical target identifiers and policy-scope references, never
    endpoints, role ARNs, tokens, or credentials. Source adapters resolve those
    deployment details server-side.
    """
    if value is None:
        return default_namespace_integration_profile()

    if hasattr(value, "model_dump"):
        raw = value.model_dump(by_alias=True, exclude_none=True)
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise IntegrationProfileError("integrationProfile must be an object")

    mode = raw.get("mode")
    if mode not in _PROFILE_MODES:
        raise IntegrationProfileError("integrationProfile.mode is invalid")

    profile_version = raw.get("profileVersion")
    if not isinstance(profile_version, str) or not _PROFILE_VERSION_RE.fullmatch(profile_version):
        raise IntegrationProfileError("integrationProfile.profileVersion is invalid")

    ontology_version = raw.get("ontologyVersion")
    if not isinstance(ontology_version, str) or not _PROFILE_VERSION_RE.fullmatch(ontology_version):
        raise IntegrationProfileError("integrationProfile.ontologyVersion is invalid")

    generation_target = raw.get("generationTarget")
    if generation_target is not None and (
        not isinstance(generation_target, str) or not _TARGET_ID_RE.fullmatch(generation_target)
    ):
        raise IntegrationProfileError("integrationProfile.generationTarget is invalid")

    targets = raw.get("sourceTargets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= _MAX_SOURCE_TARGETS:
        raise IntegrationProfileError(f"integrationProfile.sourceTargets must contain 1-{_MAX_SOURCE_TARGETS} targets")

    normalized_targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for target in targets:
        if hasattr(target, "model_dump"):
            target = target.model_dump(by_alias=True, exclude_none=True)
        if not isinstance(target, Mapping):
            raise IntegrationProfileError("integrationProfile.sourceTargets entries must be objects")

        target_id = target.get("targetId")
        if not isinstance(target_id, str) or not _TARGET_ID_RE.fullmatch(target_id):
            raise IntegrationProfileError("integrationProfile source targetId is invalid")
        if target_id in seen:
            raise IntegrationProfileError("integrationProfile source targetIds must be unique")
        seen.add(target_id)

        kind = target.get("kind")
        if kind not in _TARGET_KINDS:
            raise IntegrationProfileError("integrationProfile source target kind is invalid")
        if target.get("accessMode") != "READ_ONLY":
            raise IntegrationProfileError("integrationProfile source targets must be READ_ONLY")

        normalized: dict[str, str] = {
            "targetId": target_id,
            "kind": kind,
            "accessMode": "READ_ONLY",
        }
        scope_ref = target.get("scopeRef")
        if scope_ref is not None:
            if not isinstance(scope_ref, str) or not _TARGET_ID_RE.fullmatch(scope_ref):
                raise IntegrationProfileError("integrationProfile source scopeRef is invalid")
            normalized["scopeRef"] = scope_ref
        normalized_targets.append(normalized)

    return {
        "mode": mode,
        "profileVersion": profile_version,
        "ontologyVersion": ontology_version,
        "sourceTargets": normalized_targets,
        **({"generationTarget": generation_target} if generation_target else {}),
    }
