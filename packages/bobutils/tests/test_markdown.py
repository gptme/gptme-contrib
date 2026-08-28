"""Tests for bobutils.markdown GFM table-cell escaping.

The escaper is only correct if it agrees with the consumer that refuses to
rewrite a malformed table. Naive ``str.split('|')`` is the wrong oracle —
it does not understand ``\\|``.
"""

from __future__ import annotations

from bobutils.markdown import escape_table_cell, split_table_row_cells


def test_escapes_bare_pipe() -> None:
    assert escape_table_cell("chrome|chromium") == r"chrome\|chromium"


def test_escapes_pipe_inside_code_span() -> None:
    # GFM resolves `\|` before inline parsing, so code spans are not a shelter.
    assert escape_table_cell("`--json|--context`") == r"`--json\|--context`"


def test_is_idempotent() -> None:
    once = escape_table_cell("(AKARN|Elena V)")
    assert escape_table_cell(once) == once


def test_leaves_pipe_free_text_untouched() -> None:
    assert escape_table_cell("plain title") == "plain title"


def test_coerces_non_str() -> None:
    assert escape_table_cell(336) == "336"


def test_split_drops_delimiter_pipes() -> None:
    assert split_table_row_cells("| a | b |") == ["a", "b"]


def test_split_treats_escaped_pipe_as_one_cell() -> None:
    row = r"| 1 | **Show HN: Foo \| a tool for bars** | 100 |"
    assert split_table_row_cells(row) == [
        "1",
        r"**Show HN: Foo \| a tool for bars**",
        "100",
    ]


def test_escaped_row_parses_as_one_cell_for_the_real_consumer() -> None:
    """End-to-end: producer and consumer agree on a title that contains ``|``.

    Asserting against :func:`split_table_row_cells` (rather than ``str.split``)
    is the point — the escaper is only correct if it agrees with the consumer
    that refuses to rewrite the table.
    """
    title = "Show HN: Foo | a tool for bars"
    row = f"| 1 | **{escape_table_cell(title)}** | 100 |"
    cells = split_table_row_cells(row)
    assert len(cells) == 3
    assert cells[1] == r"**Show HN: Foo \| a tool for bars**"
    # The naive split is the failure mode this exists to prevent.
    assert len(row.split("|")) > 5
