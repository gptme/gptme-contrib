"""Markdown helpers shared by generators that write GFM tables.

An unescaped ``|`` in a table cell splits the row into phantom columns.
GFM resolves ``\\|`` *before* inline parsing, so wrapping the pipe in a
code span is not a shelter — `` `--json|--context` `` still becomes two
cells. HN titles and LLM-authored idea descriptions hit this constantly.

The producer (``escape_table_cell``) and the consumer
(``split_table_row_cells``) share one regex so they cannot drift. The
brain-repo incident this exists for: unescaped Active-Idea rows jammed
``bob-retire-graduated-ideas.service`` (2026-08-28, rows 1163/1164).
"""

from __future__ import annotations

import re

__all__ = ["escape_table_cell", "split_table_row_cells"]

# A `|` that is not already backslash-escaped. Idempotent replace of this
# pattern is what keeps re-running a generator from producing `\\\\|`.
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def escape_table_cell(text: object) -> str:
    """Escape literal ``|`` so a cell cannot introduce a phantom table column.

    Idempotent: an already-escaped ``\\|`` is left alone. Non-strings are
    coerced with ``str()`` so callers can pass scores/ids without a cast.
    """
    return _UNESCAPED_PIPE_RE.sub(r"\\|", str(text))


def split_table_row_cells(line: str) -> list[str]:
    """Return stripped content cells for a single GFM table row.

    Splits on unescaped pipes only. Leading/trailing delimiter pipes are
    dropped so ``| a | b |`` → ``['a', 'b']``. This is the consumer-side
    counterpart of :func:`escape_table_cell` — ``str.split('|')`` is the
    wrong oracle because it does not understand escaping.
    """
    parts = _UNESCAPED_PIPE_RE.split(line)
    content_cells = parts[1:-1] if line.endswith("|") else parts[1:]
    return [cell.strip() for cell in content_cells]
