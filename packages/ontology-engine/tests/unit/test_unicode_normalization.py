# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Direct unit tests for the Unicode helpers behind structured identifiers.

``to_pascal`` composes these helpers and then capitalizes, so a helper-level
regression can hide behind a caller-level assertion. Every case below pins one
helper's own contract, including the degenerate inputs that never reach a caller
with real table metadata.
"""

from __future__ import annotations

import unicodedata

import pytest
from coa_ontology.inducer.services.unicode_normalization import filter_letters_marks_numbers, width_only_nfkc

pytestmark = pytest.mark.unit

# England's subdivision flag: a base emoji (category So) plus TAG characters.
_ENGLAND_FLAG = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"


def test_runtime_unicode_version_is_pinned():
    """Identifier minting depends on the runtime's Unicode character database.

    A Python upgrade that ships a newer Unicode release can reclassify code points
    and so move an IRI. Bumping this pin must be a reviewed decision.
    """
    assert unicodedata.unidata_version == "15.0.0"


@pytest.mark.parametrize("normalize", [width_only_nfkc, filter_letters_marks_numbers])
def test_empty_string_round_trips(normalize):
    assert normalize("") == ""


class TestWidthOnlyNfkc:
    """Only U+FFxx width forms fold, and exceptional targets stop at one step."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("￣", "¯", id="fullwidth-macron-to-macron"),
            pytest.param("ﾠ", "ㅤ", id="halfwidth-hangul-filler"),
            pytest.param("ﾡ", "ㄱ", id="halfwidth-kiyeok-to-compatibility-jamo"),
            pytest.param("ￂ", "ㅏ", id="halfwidth-a-to-compatibility-jamo"),
            pytest.param("ￊ", "ㅕ", id="halfwidth-yeo-to-compatibility-jamo"),
            pytest.param("ￒ", "ㅛ", id="halfwidth-yo-to-compatibility-jamo"),
            pytest.param("ￚ", "ㅡ", id="halfwidth-eu-to-compatibility-jamo"),
        ],
    )
    def test_exceptional_targets_stop_at_the_direct_mapping(self, value, expected):
        """Plain NFKC overshoots on these and would change identity."""
        assert width_only_nfkc(value) == expected
        assert unicodedata.normalize("NFKC", value) != expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("Ａ", "A", id="fullwidth-latin-capital"),
            pytest.param("顧客ＩＤ", "顧客ID", id="fullwidth-ascii-in-a-japanese-name"),
            pytest.param("ｶﾞ", "ガ", id="halfwidth-ka-plus-dakuten-composes"),
        ],
    )
    def test_ordinary_width_forms_use_nfkc(self, value, expected):
        assert width_only_nfkc(value) == expected

    @pytest.mark.parametrize("value", ["℃", "™", "ﬁ"])
    def test_compatibility_forms_outside_the_width_block_survive(self, value):
        """DEGREE CELSIUS, TRADE MARK and the fi ligature are not width forms."""
        assert width_only_nfkc(value) == value

    def test_ascii_is_unchanged(self):
        ascii_chars = "".join(chr(c) for c in range(128))
        assert width_only_nfkc(ascii_chars) == ascii_chars


class TestFilterLettersMarksNumbers:
    """Letters and numbers in every script; marks and format characters only behind a base."""

    @pytest.mark.parametrize("value", ["設備id", "メールアドレス", "고객이름", "Название", "कर्मचारी", "อุณหภูมิ"])
    def test_letters_numbers_and_attached_marks_are_retained(self, value):
        assert filter_letters_marks_numbers(value) == value

    def test_ascii_keeps_exactly_alphanumerics(self):
        ascii_chars = "".join(chr(c) for c in range(128))
        expected = "".join(c for c in ascii_chars if c.isascii() and c.isalnum())
        assert filter_letters_marks_numbers(ascii_chars) == expected
        assert filter_letters_marks_numbers(ascii_chars, keep_underscore=True) == "".join(
            c for c in ascii_chars if c.isalnum() or c == "_"
        )

    def test_a_value_of_only_marks_becomes_empty(self):
        assert filter_letters_marks_numbers("\u0300\u0301\u0302") == ""

    def test_symbols_and_punctuation_are_dropped(self):
        assert filter_letters_marks_numbers("温度℃・（有効）%") == "温度有効"

    def test_tag_characters_go_with_a_base_that_was_not_retained(self):
        """The flag's base is a symbol, so it and its tags leave together."""
        assert filter_letters_marks_numbers(_ENGLAND_FLAG) == ""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("\u200d", "", id="orphan-zwj"),
            pytest.param("\u200c", "", id="orphan-zwnj"),
            pytest.param("a\u200db", "a\u200db", id="zwj-between-letters"),
        ],
    )
    def test_join_controls_need_a_retained_base(self, value, expected):
        assert filter_letters_marks_numbers(value) == expected

    def test_underscore_is_not_a_base_for_an_attached_mark(self):
        """``keep_underscore`` retains the separator without making it a base."""
        assert filter_letters_marks_numbers("a_\u0300", keep_underscore=True) == "a_"
