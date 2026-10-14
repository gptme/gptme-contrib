"""Tests for bobutils.frontmatter.split_frontmatter."""

import pytest
from bobutils.frontmatter import split_frontmatter


def test_basic_split():
    text = "---\ntitle: Hello\n---\n\nBody text."
    fm, body = split_frontmatter(text)
    assert fm == "title: Hello\n"
    assert body == "\nBody text."


def test_dash_inside_value_does_not_shear():
    """A '---' substring inside a frontmatter value must not close the fence."""
    text = "---\nprobe: echo '---' | head\n---\n\nBody."
    fm, body = split_frontmatter(text)
    assert "probe: echo '---'" in fm
    assert body == "\nBody."


def test_multiline_frontmatter():
    text = "---\na: 1\nb: 2\nc: 3\n---\nBody line."
    fm, body = split_frontmatter(text)
    assert fm == "a: 1\nb: 2\nc: 3\n"
    assert body == "Body line."


def test_empty_body():
    text = "---\nkey: val\n---\n"
    fm, body = split_frontmatter(text)
    assert fm == "key: val\n"
    assert body == ""


def test_missing_open_fence_raises():
    with pytest.raises(ValueError, match="does not begin"):
        split_frontmatter("title: no fence\n---\nbody")


def test_missing_close_fence_raises():
    with pytest.raises(ValueError, match="no closing"):
        split_frontmatter("---\ntitle: unterminated\n")


def test_empty_frontmatter():
    text = "---\n---\nBody only."
    fm, body = split_frontmatter(text)
    assert fm == ""
    assert body == "Body only."


def test_indented_dashes_in_block_scalar_do_not_close_fence():
    """An indented '---' inside a YAML block scalar must not close the fence.

    A bare '---' at zero-indent IS a YAML document separator and cannot appear
    as a legal value in valid frontmatter. Indented occurrences ('  ---') are
    not equal to '---' so the line-by-line check correctly ignores them.
    """
    text = "---\nprobe: |\n  ---\n  more\n---\nBody."
    fm, body = split_frontmatter(text)
    assert "  ---" in fm
    assert body == "Body."
