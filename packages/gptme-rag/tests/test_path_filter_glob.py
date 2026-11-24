"""``path_filters`` globs match within one path segment (gptme/gptme-contrib#2356, Q4)."""

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
        ("/repo/src/sub/deep.py", "src/?ub.py"),
    ],
)
def test_star_does_not_cross_directories(source, pattern):
    assert not _matches(source, pattern)


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
    ],
)
def test_segment_globs_still_match(source, pattern):
    assert _matches(source, pattern)
