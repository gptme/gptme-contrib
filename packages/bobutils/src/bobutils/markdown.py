"""Markdown helpers shared by generators that write GFM tables.

An unescaped ``|`` in a table cell splits the row into phantom columns.
GFM resolves ``\\|`` *before* inline parsing, so wrapping the pipe in a
code span is not a shelter — `` `--json|--context` `` still becomes two
cells. HN titles and LLM-authored idea descriptions hit this constantly.

The producer (``escape_table_cell``) and the consumer
(``split_table_row_cells``) share one scanner so they cannot drift. A
negative-lookbehind regex is the wrong scanner: ``(?<!\\\\)|`` treats an
even backslash run as an escape, but GFM renders ``\\\\|`` as a literal
backslash plus a column delimiter. The brain-repo incident this exists
for: unescaped Active-Idea rows jammed
``bob-retire-graduated-ideas.service`` (2026-08-28, rows 1163/1164).
"""

from __future__ import annotations

__all__ = ["escape_table_cell", "split_table_row_cells"]


def _unescaped_pipe_indices(text: str) -> list[int]:
    """Return indices of ``|`` that GFM treats as delimiters.

    A backslash escapes the next character, so a pipe is unescaped iff
    the run of backslashes immediately before it has even length
    (including zero). Walking pairs is what keeps ``\\\\|`` (even) a
    delimiter and ``\\|`` (odd) a literal pipe.
    """
    indices: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if text[i] == "|":
            indices.append(i)
        i += 1
    return indices


def escape_table_cell(text: object) -> str:
    """Escape literal ``|`` so a cell cannot introduce a phantom table column.

    Idempotent: an already-escaped ``\\|`` is left alone. Non-strings are
    coerced with ``str()`` so callers can pass scores/ids without a cast.
    """
    s = str(text)
    idxs = _unescaped_pipe_indices(s)
    if not idxs:
        return s
    parts: list[str] = []
    last = 0
    for i in idxs:
        parts.append(s[last:i])
        parts.append("\\|")
        last = i + 1
    parts.append(s[last:])
    return "".join(parts)


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
    idxs = _unescaped_pipe_indices(line)
    parts: list[str] = []
    last = 0
    for i in idxs:
        parts.append(line[last:i])
        last = i + 1
    parts.append(line[last:])
    # Unescaped delimiter pipes produce empty split parts. Drop those only —
    # not "the first part" / "the last part" by position. A row with no
    # leading pipe, or a last cell that ends with ``\\|``, must keep that cell.
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return [cell.strip() for cell in parts]
