"""Canonical slug helper.

Replaces ~14 per-script ``slugify`` definitions whose core logic was identical
(lowercase → non-alphanumeric runs → hyphens → strip) but varied in max_len
truncation and empty-input fallback.

Markdown-aware callers (session_to_blog_draft.py, extract-candidates.py) keep
a local strip step piped into this canonical helper.
"""

from __future__ import annotations

import re

__all__ = ["slugify"]


def slugify(text: str, *, max_len: int | None = None, fallback: str = "item") -> str:
    """Convert text to a filesystem-safe hyphenated slug.

    Lowercases, collapses non-alphanumeric runs to single hyphens, strips
    leading/trailing hyphens. Optionally truncates to max_len, stripping any
    trailing hyphen introduced by the cut. Returns fallback when result is empty.

    Args:
        text: Input text.
        max_len: Maximum slug length. No limit when None.
        fallback: Returned when the slug would otherwise be empty.

    Returns:
        A slug string, or fallback.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")
    if max_len is not None:
        slug = slug[:max_len].rstrip("-")
    return slug or fallback
