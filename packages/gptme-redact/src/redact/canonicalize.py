"""Canonicalization helper for Unicode-obfuscated prompt injection detection.

This module provides detection-only canonicalization for prompt-injection scanning.
It removes default-ignorable characters and maps Unicode confusables, enabling the
scanner to detect evasions that bypass regex matching via zero-width chars or homoglyphs.

CRITICAL: Canonicalization preserves the original payload and does NOT mutate
content delivered to callers. This is advisory scanning only.
"""

from __future__ import annotations

import unicodedata
from typing import NamedTuple

from redact import Finding, scan_prompt_injection


class CanonicalResult(NamedTuple):
    """Result of canonicalization with comparison."""

    original_text: str
    canonical_text: str
    original_findings: list[Finding]
    canonical_findings: list[Finding]
    new_detections: list[Finding]  # Findings in canonical but not original
    evaded_patterns: list[str]  # Categories of patterns that were evaded


# Unicode characters that are default-ignorable or have no width.
# These are commonly used to split injection patterns to evade regex matching.
# Reference: https://www.unicode.org/reports/tr37/
_DEFAULT_IGNORABLE_CHARS = {
    "​",  # Zero-width space
    "‌",  # Zero-width non-joiner
    "‍",  # Zero-width joiner
    "‎",  # Left-to-right mark
    "‏",  # Right-to-left mark
    "؜",  # Arabic letter mark
    "᠎",  # Mongolian vowel separator
    "⁠",  # Word joiner
    "⁡",  # Function application
    "⁢",  # Invisible times
    "⁣",  # Invisible separator
    "⁤",  # Invisible plus
    "﻿",  # Zero-width no-break space
    "͏",  # Combining grapheme joiner
    "឴",  # Khmer vowel inherent aq
    "឵",  # Khmer vowel inherent aa
    "᠋",  # Mongolian free variation selector 1
    "᠌",  # Mongolian free variation selector 2
    "᠍",  # Mongolian free variation selector 3
    " ",  # Hair space (visible but minimal)
    "‪",  # Left-to-right embedding
    "‫",  # Right-to-left embedding
    "‬",  # Pop directional formatting
    "‭",  # Left-to-right override
    "‮",  # Right-to-left override
    "⁦",  # Left-to-right isolate
    "⁧",  # Right-to-left isolate
    "⁨",  # First strong isolate
    "⁩",  # Pop directional isolate
}

# Conservative mapping of Unicode confusables to ASCII equivalents.
# Focused on high-severity injection patterns and common Latin/Cyrillic/Greek homoglyphs.
# Source: https://www.unicode.org/reports/tr39/
_CONFUSABLE_MAP = {
    # Cyrillic lookalikes (extremely common in homoglyph attacks)
    "а": "a",  # Cyrillic 'a' → Latin 'a'
    "е": "e",  # Cyrillic 'e' → Latin 'e'
    "о": "o",  # Cyrillic 'o' → Latin 'o'
    "р": "p",  # Cyrillic 'r' → Latin 'p'
    "с": "c",  # Cyrillic 's' → Latin 'c'
    "х": "x",  # Cyrillic 'h' → Latin 'x'
    "у": "y",  # Cyrillic 'u' → Latin 'y'
    "н": "n",  # Cyrillic 'n' → Latin 'n'
    "в": "b",  # Cyrillic 'v' → Latin 'b'
    "ь": "b",  # Cyrillic soft sign → 'b'
    "М": "M",  # Cyrillic 'M' → Latin 'M'
    "Н": "N",  # Cyrillic 'N' → Latin 'N'
    "О": "O",  # Cyrillic 'O' → Latin 'O'
    "Р": "P",  # Cyrillic 'P' → Latin 'P'
    "С": "C",  # Cyrillic 'C' → Latin 'C'
    "Т": "T",  # Cyrillic 'T' → Latin 'T'
    "У": "Y",  # Cyrillic 'U' → Latin 'Y'
    "Х": "X",  # Cyrillic 'X' → Latin 'X'
    "В": "B",
    "Ё": "E",
    # Greek lookalikes
    "α": "a",  # Greek alpha → Latin 'a'
    "ο": "o",  # Greek omicron → Latin 'o'
    "ρ": "p",  # Greek rho → Latin 'p'
    "ν": "v",  # Greek nu → Latin 'v'
    "τ": "t",  # Greek tau → Latin 't'
    "χ": "x",  # Greek chi → Latin 'x'
    # Latin Extended-A diacritics commonly used to split words
    "ā": "a",  # Latin 'a' with macron
    "ē": "e",  # Latin 'e' with macron
    "ī": "i",  # Latin 'i' with macron
    "ō": "o",  # Latin 'o' with macron
    "ū": "u",  # Latin 'u' with macron
    # Mathematical alphanumeric symbols (often used in homoglyph attacks)
    # Bold variants
    "𝐚": "a",
    "𝐛": "b",
    "𝐜": "c",
    "𝐝": "d",
    "𝐞": "e",
    "𝐟": "f",
    "𝐠": "g",
    "𝐡": "h",
    "𝐢": "i",
    "𝐣": "j",
    "𝐤": "k",
    "𝐥": "l",
    "𝐦": "m",
    "𝐧": "n",
    "𝐨": "o",
    "𝐩": "p",
    "𝐪": "q",
    "𝐫": "r",
    "𝐬": "s",
    "𝐭": "t",
    "𝐮": "u",
    "𝐯": "v",
    "𝐰": "w",
    "𝐱": "x",
    "𝐲": "y",
    "𝐳": "z",
    "𝐀": "A",
    "𝐁": "B",
    "𝐂": "C",
    "𝐃": "D",
    "𝐄": "E",
    "𝐅": "F",
    "𝐆": "G",
    "𝐇": "H",
    "𝐈": "I",
    "𝐉": "J",
    "𝐊": "K",
    "𝐋": "L",
    "𝐌": "M",
    "𝐍": "N",
    "𝐎": "O",
    "𝐏": "P",
    "𝐐": "Q",
    "𝐑": "R",
    "𝐒": "S",
    "𝐓": "T",
    "𝐔": "U",
    "𝐕": "V",
    "𝐖": "W",
    "𝐗": "X",
    "𝐘": "Y",
    "𝐙": "Z",
    # Fullwidth variants (common in non-Latin scripts)
    "ａ": "a",
    "ｂ": "b",
    "ｃ": "c",
    "ｄ": "d",
    "ｅ": "e",
    "ｆ": "f",
    "ｇ": "g",
    "ｈ": "h",
    "ｉ": "i",
    "ｊ": "j",
    "ｋ": "k",
    "ｌ": "l",
    "ｍ": "m",
    "ｎ": "n",
    "ｏ": "o",
    "ｐ": "p",
    "ｑ": "q",
    "ｒ": "r",
    "ｓ": "s",
    "ｔ": "t",
    "ｕ": "u",
    "ｖ": "v",
    "ｗ": "w",
    "ｘ": "x",
    "ｙ": "y",
    "ｚ": "z",
    "Ａ": "A",
    "Ｂ": "B",
    "Ｃ": "C",
    "Ｄ": "D",
    "Ｅ": "E",
    "Ｆ": "F",
    "Ｇ": "G",
    "Ｈ": "H",
    "Ｉ": "I",
    "Ｊ": "J",
    "Ｋ": "K",
    "Ｌ": "L",
    "Ｍ": "M",
    "Ｎ": "N",
    "Ｏ": "O",
    "Ｐ": "P",
    "Ｑ": "Q",
    "Ｒ": "R",
    "Ｓ": "S",
    "Ｔ": "T",
    "Ｕ": "U",
    "Ｖ": "V",
    "Ｗ": "W",
    "Ｘ": "X",
    "Ｙ": "Y",
    "Ｚ": "Z",
}


def remove_default_ignorable_chars(text: str) -> str:
    """Replace default-ignorable and zero-width characters with spaces.

    These characters are commonly used to split injection patterns:
    - Zero-width spaces/joiners (U+200B, U+200C, U+200D)
    - Directional marks and isolates
    - Invisible operators and separators

    Zero-width characters are replaced with spaces to preserve word boundaries
    for regex matching. Other formatting characters are removed.

    The original payload is preserved; this is advisory scanning only.
    """
    result = []
    for c in text:
        if c in _DEFAULT_IGNORABLE_CHARS:
            # Replace zero-width characters with space to preserve word boundaries
            if c in ("​", "‌", "‍", "⁠"):  # Zero-width spaces and joiners
                result.append(" ")
            # Directional marks, isolates: just remove (they don't represent content)
            # else: skip
        else:
            result.append(c)
    return "".join(result)


def map_confusables(text: str) -> str:
    """Map Unicode confusables to ASCII equivalents.

    Converts:
    - Cyrillic lookalikes (а→a, о→o, etc.)
    - Greek lookalikes (α→a, ρ→p, etc.)
    - Mathematical alphanumeric symbols
    - Fullwidth variants

    This is a conservative allowlist covering high-severity injection phrases.
    The original payload is preserved; this is advisory scanning only.
    """
    result = text
    for source, target in _CONFUSABLE_MAP.items():
        result = result.replace(source, target)
    return result


def canonicalize_for_injection_scan(text: str) -> tuple[str, str]:
    """Canonicalize text for injection pattern scanning.

    Returns:
        (original_text, canonical_text) where canonical_text has:
        - Default-ignorable characters removed
        - Unicode confusables mapped to ASCII equivalents

    The original payload is preserved and unchanged.
    This is detection-only; callers receive the original text unchanged.
    """
    canonical = remove_default_ignorable_chars(text)
    canonical = map_confusables(canonical)
    # Normalize combining marks to NFC form to handle nested diacritics
    canonical = unicodedata.normalize("NFC", canonical)
    return text, canonical


def scan_with_canonicalization(text: str) -> CanonicalResult:
    """Scan text for injection patterns using both raw and canonical forms.

    Compares findings between the raw text and a canonicalized version
    (zero-width chars removed, confusables mapped). This detects evasion
    attempts that would bypass simple regex matching.

    Args:
        text: Input text to scan (preserved unchanged)

    Returns:
        CanonicalResult with:
        - original_text: Unchanged input
        - canonical_text: Canonicalized version
        - original_findings: Patterns found in raw text
        - canonical_findings: Patterns found in canonical text
        - new_detections: Findings in canonical but not in raw (likely evasion)
        - evaded_patterns: Category names of evaded patterns
    """
    original_text, canonical_text = canonicalize_for_injection_scan(text)

    original_findings = scan_prompt_injection(original_text)
    canonical_findings = scan_prompt_injection(canonical_text)

    # Identify patterns that were evaded in the original but detected in canonical
    original_cats = {f.category for f in original_findings}
    canonical_cats = {f.category for f in canonical_findings}
    evaded_cats = canonical_cats - original_cats

    new_detections = [f for f in canonical_findings if f.category in evaded_cats]
    evaded_patterns = sorted(evaded_cats)

    return CanonicalResult(
        original_text=original_text,
        canonical_text=canonical_text,
        original_findings=original_findings,
        canonical_findings=canonical_findings,
        new_detections=new_detections,
        evaded_patterns=evaded_patterns,
    )
