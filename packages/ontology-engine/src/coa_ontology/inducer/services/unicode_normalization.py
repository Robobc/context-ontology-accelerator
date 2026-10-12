# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unicode helpers for minting ontology identifiers from catalog names.

Category membership comes from the runtime's :mod:`unicodedata`. The service
image and CI both run Python 3.12, which ships Unicode 15.0;
``test_unicode_normalization`` pins that version so a runtime upgrade that could
move an identifier is a deliberate, reviewed change rather than a silent one.
"""

from __future__ import annotations

import unicodedata

_JOIN_CONTROLS = frozenset({"\u200c", "\u200d"})
_TAG_CHARACTER_MIN = 0xE0020
_TAG_CHARACTER_MAX = 0xE007F


def _fold_width_character(character: str) -> str:
    """Return the direct ordinary-width counterpart of one U+FFxx form.

    NFKC recursively normalizes the mapped target, which is too broad here:
    FULLWIDTH MACRON becomes SPACE + COMBINING MACRON and halfwidth Hangul
    becomes conjoining Jamo that can compose into a syllable. Preserve the
    direct compatibility-Jamo/macron targets, and use NFKC for the remaining
    width forms where its result is already the direct mapping.
    """
    code_point = ord(character)
    if code_point == 0xFFA0:
        return "\u3164"
    for start, end, target_start in (
        (0xFFA1, 0xFFBE, 0x3131),
        (0xFFC2, 0xFFC7, 0x314F),
        (0xFFCA, 0xFFCF, 0x3155),
        (0xFFD2, 0xFFD7, 0x315B),
        (0xFFDA, 0xFFDC, 0x3161),
    ):
        if start <= code_point <= end:
            return chr(target_start + code_point - start)
    if code_point == 0xFFE3:
        return "\u00af"
    return unicodedata.normalize("NFKC", character)


def width_only_nfkc(value: str) -> str:
    """Fold only Unicode halfwidth/fullwidth forms, then recompose.

    Unrestricted NFKC is destructive for ontology identity: it expands symbols
    such as ``℃`` and ``™`` into ordinary text. Only U+FFxx width forms are
    folded, and exceptional targets such as FULLWIDTH MACRON and halfwidth
    Hangul stop at their direct ordinary-width mapping instead of receiving a
    second compatibility normalization. The final NFC pass composes base
    characters with folded Katakana dakuten marks.
    """
    normalized = unicodedata.normalize("NFC", value)
    folded = "".join(
        _fold_width_character(character) if "\uff00" <= character <= "\uffef" else character for character in normalized
    )
    return unicodedata.normalize("NFC", folded)


def filter_letters_marks_numbers(value: str, *, keep_underscore: bool = False) -> str:
    """Keep letters, numbers, and marks attached to a retained base character.

    Letters (``L*``) and numbers (``N*``) are always retained, in every script.
    ZWJ/ZWNJ and Unicode TAG characters are retained after a base because they
    distinguish joined-script graphemes and subdivision flags. A mark or TAG
    character without a retained base is discarded, so no key is made of
    format characters alone.
    """
    filtered: list[str] = []
    has_base = False

    for character in value:
        category = unicodedata.category(character)
        if category[0] in ("L", "N"):
            filtered.append(character)
            has_base = True
            continue
        if category[0] == "M":
            if has_base:
                filtered.append(character)
            continue
        if character in _JOIN_CONTROLS and has_base:
            filtered.append(character)
            continue
        if _TAG_CHARACTER_MIN <= ord(character) <= _TAG_CHARACTER_MAX and has_base:
            filtered.append(character)
            continue
        if keep_underscore and character == "_":
            filtered.append(character)
        has_base = False

    return "".join(filtered)
