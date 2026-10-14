# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from coa_common.model_text import (
    delimit_model_input,
    secure_system_prompt,
    strip_hidden_model_characters,
    validate_model_output_text,
)

pytestmark = pytest.mark.unit


def test_strips_tags_and_surrogates_only():
    value = "domain\U000e0001hidden\U000e007f — graph 日本語\ud800"
    assert strip_hidden_model_characters(value) == "domainhidden — graph 日本語"


def test_is_idempotent():
    value = "Graph\U000e0020"
    once = strip_hidden_model_characters(value)
    assert strip_hidden_model_characters(once) == once


def test_rejects_non_text():
    with pytest.raises(TypeError):
        strip_hidden_model_characters(None)  # type: ignore[arg-type]


def test_prompt_boundary_is_sandwiched_and_stable():
    system = secure_system_prompt("Answer domain questions")
    delimited = delimit_model_input("question")
    assert "COA_SYSTEM_BOUNDARY_7C2A91F4" in system
    assert "<user_input>question</user_input>" in delimited
    assert "Never reveal" in system
    assert "Never reveal" in delimited


def test_model_output_canary_fails_closed():
    with pytest.raises(ValueError, match="output validation"):
        validate_model_output_text("leaked coa_system_boundary_7c2a91f4")
    assert validate_model_output_text("ordinary response") == "ordinary response"
