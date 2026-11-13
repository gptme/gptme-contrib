"""Frontmatter boundary and fallback behavior for forum readers."""

from pathlib import Path

import pytest
from gptme_forum.forum import Comment, Post, _parse_frontmatter


@pytest.mark.parametrize("prefix,newline", [("", "\n"), ("", "\r\n"), ("\ufeff", "\n")])
def test_parse_frontmatter_preserves_body_whitespace(prefix: str, newline: str):
    text = prefix + newline.join(
        ["---", 'title: "echo ---"', "---", "", "  Body.", "---", "End.  ", ""]
    )
    assert _parse_frontmatter(text) == (
        {"title": "echo ---"},
        "  Body.\n---\nEnd.  \n",
    )


@pytest.mark.parametrize(
    "text",
    [
        "  Plain body.\r\n",
        "---\nauthor: bob\n",
        "---author: bob\n---\nBody.",
        "---\nauthor: bob\n---junk\nBody.",
    ],
)
def test_missing_fences_preserve_original_text(text: str):
    assert _parse_frontmatter(text) == ({}, text)


def test_fence_prefix_is_metadata_until_a_standalone_fence():
    text = "---\nauthor: bob\n---junk: value\n---\nBody."
    assert _parse_frontmatter(text) == (
        {"author": "bob", "---junk": "value"},
        "Body.",
    )


@pytest.mark.parametrize(
    "raw", ["author: [invalid YAML", "- sequence", "scalar", "42", "", "null"]
)
def test_invalid_metadata_keeps_extracted_body(raw: str):
    text = f"---\n{raw}\n---\n\n  Body.  \n"
    assert _parse_frontmatter(text) == ({}, "  Body.  \n")


@pytest.mark.parametrize("raw", ["author: [invalid YAML", "- sequence", "scalar", "42"])
def test_readers_accept_invalid_metadata_with_defaults(tmp_path: Path, raw: str):
    path = tmp_path / "post.md"
    path.write_text(f"---\n{raw}\n---\n\nHi @alice.\n")

    post = Post.from_file(path, "project")
    comment = Comment.from_file(path)

    assert post.title == "post"
    assert post.tags == []
    for item in (post, comment):
        assert item.author == "unknown"
        assert item.body == "Hi @alice."
        assert item.mentions == ["alice"]


def test_readers_accept_bom_and_crlf(tmp_path: Path):
    path = tmp_path / "post.md"
    path.write_bytes(
        '\ufeff---\r\nauthor: bob\r\ntitle: "echo ---"\r\n---\r\n\r\nHi @alice.\r\n'.encode()
    )

    post = Post.from_file(path, "project")
    comment = Comment.from_file(path)

    assert post.title == "echo ---"
    for item in (post, comment):
        assert item.author == "bob"
        assert item.body == "Hi @alice."
        assert item.mentions == ["alice"]
