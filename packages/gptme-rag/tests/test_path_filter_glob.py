"""``path_filters`` globs match within one path segment (gptme/gptme-contrib#2356, Q4)."""

import time

import pytest
from gptme_rag.indexing.document import Document
from gptme_rag.indexing.indexer import Indexer


def _matches(source: str, pattern: str) -> bool:
    doc = Document(content="x", metadata={"source": source})
    indexer = Indexer.__new__(Indexer)  # _matches_paths needs no backend
    return indexer._matches_paths(doc, path_filters=(pattern,))


@pytest.mark.parametrize(
    "source,pattern",
    [
        ("/repo/src/sub/deep.py", "src/*.py"),
        ("/repo/docs/a/b.md", "docs/*"),
        ("/repo/src/sub/deep.py", "src/sub?deep.py"),
        ("/repo/src/sub/deep.py", "src/sub[!x]deep.py"),
        ("/repo/src/sub/deep.py", "src/sub[--0]deep.py"),
        ("/repo/src/sub/deep.py", "src/**deep.py"),
        ("/repo/src/sub/inner/deep.py", "src/s**/deep.py"),
    ],
)
def test_star_does_not_cross_directories(source, pattern):
    assert not _matches(source, pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        "src/[!].py",
        "src/[z-a].py",
    ],
)
def test_malformed_character_class_does_not_raise(pattern):
    """A bad class must not crash the search (fnmatch treats it as a literal)."""
    assert _matches("/repo/src/a.py", pattern) is False


def test_leading_caret_is_a_literal_in_a_class():
    """fnmatch treats '[^ab]' as matching a literal '^', not as negation."""
    assert _matches("/repo/src/^.py", "src/[^ab].py")
    assert not _matches("/repo/src/c.py", "src/[^ab].py")


@pytest.mark.parametrize(
    "source,pattern",
    [
        ("/repo/src/top.py", "src/*.py"),
        ("/repo/src/sub/deep.py", "src/**/*.py"),
        ("/repo/src/deep.py", "src/**/*.py"),
        ("/repo/docs/a/b.md", "docs/**"),
        ("/repo/src/sub/deep.py", "*.py"),
        ("/repo/src/sub/deep.py", "sub/deep.py"),
        ("/repo/src/sub/deep.py", "/repo/src/*/deep.py"),
        ("/repo/src/top.py", "src/[st]op.py"),
        ("/repo/src/sub-deep.py", "src/sub[!x]deep.py"),
        ("/repo/src/sub-deep.py", "src/sub[--0]deep.py"),
        ("/repo/src/deep.py", "src/**deep.py"),
        ("/repo/src/sub/deep.py", "src/s**/deep.py"),
        ("/repo/src/.hidden.py", "src/*.py"),
        ("/repo/src/].py", "src/[]].py"),
        ("/repo/src/[.py", "src/[[].py"),
    ],
)
def test_segment_globs_still_match(source, pattern):
    assert _matches(source, pattern)


def test_literal_closing_bracket_member_after_negation():
    """``[!]]`` is a negated class containing a literal ']' (fnmatch rule)."""
    assert _matches("/repo/src/a.py", "src/[!]].py")
    assert not _matches("/repo/src/].py", "src/[!]].py")


def test_pathological_star_pattern_matches_in_linear_time():
    """Overlapping stars must not trigger exponential regex backtracking."""
    pattern = "src/" + "*a" * 10 + "*.py"
    source = "/repo/src/" + "a" * 24 + ".txt"
    start = time.perf_counter()
    assert not _matches(source, pattern)
    assert time.perf_counter() - start < 0.5
