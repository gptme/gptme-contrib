"""YAML frontmatter splitter for Markdown task/skill/lesson files.

The naive ``text.split("---", 2)`` splits on any three-dash run including
those inside frontmatter values (e.g. a ``probe:`` field whose value
contains ``---``). Session 4788 hit this: the first strip pass left two
orphaned probe lines that only a re-parse caught. The correct closing
fence is ``\\n---\\n`` — a line whose *entire* content is ``---``.
"""

from __future__ import annotations

__all__ = ["split_frontmatter"]

_OPEN_FENCE = "---\n"


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split a Markdown file with YAML frontmatter into (frontmatter, body).

    The frontmatter is everything between the opening ``---`` fence and the
    closing ``---`` fence (each ``---`` must occupy its own line). The body
    is everything after the closing fence.

    Uses a line-by-line scan so that a ``---`` *substring* inside a value
    (e.g. ``probe: echo '---' | head``) does not shear the body into the
    frontmatter. The naive ``text.split("---", 2)`` would split on such a
    substring; this implementation does not.

    Returns:
        (frontmatter_text, body_text) — frontmatter_text does NOT include
        the surrounding ``---`` fences; body_text starts after the fence.

    Raises:
        ValueError: if the text does not start with ``---\\n`` or if no
            closing ``---`` fence line is found.
    """
    # Normalize CRLF so Windows checkouts and git autocrlf=true files parse cleanly.
    # Also strip a leading UTF-8 BOM if present.
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    if not text.startswith(_OPEN_FENCE):
        raise ValueError("text does not begin with a frontmatter fence (---\\n)")

    rest = text[len(_OPEN_FENCE) :]
    lines = rest.split("\n")
    for i, line in enumerate(lines):
        if line == "---":
            fm_lines = lines[:i]
            frontmatter = "\n".join(fm_lines)
            if frontmatter:
                frontmatter += "\n"
            body = "\n".join(lines[i + 1 :])
            return frontmatter, body

    raise ValueError("no closing frontmatter fence (--- on its own line) found")
