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

    Splits on unescaped pipes only. Empty leading/trailing split parts are
    the delimiter pipes, so ``| a | b |`` → ``['a', 'b']``. A raw
    ``str.endswith('|')`` slice is the wrong test: a cell that *ends* with
    an escaped ``\\|`` also ends with ``|``, and slicing it off drops the
    cell. Do not use ``str.split('|')`` either — it does not understand
    escaping. This is the consumer-side counterpart of
    :func:`escape_table_cell`.
    """
    parts = _UNESCAPED_PIPE_RE.split(line)
    # Unescaped delimiter pipes produce empty split parts. Drop those only —
    # not "the first part" / "the last part" by position. A row with no
    # leading pipe, or a last cell that ends with ``\\|``, must keep that cell.
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return [cell.strip() for cell in parts]
