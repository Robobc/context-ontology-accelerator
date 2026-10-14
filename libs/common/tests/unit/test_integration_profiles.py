# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from coa_common.integration_profiles import (
    IntegrationProfileError,
    default_namespace_integration_profile,
    normalize_namespace_integration_profile,
)

pytestmark = pytest.mark.unit


def test_default_profile_is_fresh_and_read_only():
    first = default_namespace_integration_profile()
    second = default_namespace_integration_profile()
    first["sourceTargets"][0]["targetId"] = "changed"
    assert second == {
        "mode": "ACCELERATE",
        "profileVersion": "1",
        "ontologyVersion": "legacy-unversioned",
        "sourceTargets": [{"targetId": "coa-graph", "kind": "COA_GRAPH", "accessMode": "READ_ONLY"}],
        "generationTarget": "bedrock",
    }


def test_profile_projects_only_contract_fields():
    normalized = normalize_namespace_integration_profile(
        {
            "mode": "FEDERATE",
            "profileVersion": "profile-v1",
            "ontologyVersion": "ontology-2026-10-14",
            "generationTarget": "portkey",
            "sourceTargets": [
                {
                    "targetId": "external-graph",
                    "kind": "EXTERNAL_GRAPH",
                    "accessMode": "READ_ONLY",
                    "scopeRef": "external-graph-scope",
                    "endpoint": "https://should-not-be-persisted.example",
                    "token": "should-not-be-persisted",
                }
            ],
            "credentials": "should-not-be-persisted",
        }
    )
    assert normalized == {
        "mode": "FEDERATE",
        "profileVersion": "profile-v1",
        "ontologyVersion": "ontology-2026-10-14",
        "generationTarget": "portkey",
        "sourceTargets": [
            {
                "targetId": "external-graph",
                "kind": "EXTERNAL_GRAPH",
                "accessMode": "READ_ONLY",
                "scopeRef": "external-graph-scope",
            }
        ],
    }


@pytest.mark.parametrize(
    "profile",
    [
        {"mode": "UNKNOWN", "profileVersion": "1", "sourceTargets": [{}]},
        {"mode": "ADOPT", "profileVersion": "1", "sourceTargets": []},
        {
            "mode": "ADOPT",
            "profileVersion": "1",
            "sourceTargets": [{"targetId": "db", "kind": "GOVERNED_QUERY", "accessMode": "READ_WRITE"}],
        },
        {
            "mode": "ADOPT",
            "profileVersion": "1",
            "sourceTargets": [
                {"targetId": "db", "kind": "GOVERNED_QUERY", "accessMode": "READ_ONLY"},
                {"targetId": "db", "kind": "MANAGED_KB", "accessMode": "READ_ONLY"},
            ],
        },
    ],
)
def test_invalid_profile_fails_closed(profile):
    with pytest.raises(IntegrationProfileError):
        normalize_namespace_integration_profile(profile)
