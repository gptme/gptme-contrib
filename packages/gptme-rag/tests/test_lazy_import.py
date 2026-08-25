"""Test that gptme_rag is importable without heavy deps (chromadb/sentence_transformers).

Phase 3 prerequisite: match-lessons.py and similar hooks must be able to
``import gptme_rag`` in the brain runtime without pulling in chromadb +
torch (which adds several GB and ~10s to every prompt-submit hook invocation).
"""

from __future__ import annotations

import sys


def test_plain_import_does_not_pull_chromadb() -> None:
    """``import gptme_rag`` must not import chromadb or sentence_transformers."""
    # Remove any prior import so the test is isolated
    mods_to_drop = [k for k in sys.modules if k.startswith("gptme_rag")]
    for mod in mods_to_drop:
        del sys.modules[mod]

    import gptme_rag  # noqa: F401

    heavy = [m for m in sys.modules if m in ("chromadb", "sentence_transformers")]
    assert not heavy, f"Heavy deps imported after plain import: {heavy}"


def test_lesson_matcher_functions_eagerly_importable() -> None:
    """Lesson-matching surface must be importable without instantiating the dense backend."""
    from gptme_rag import (
        filter_by_session_category,
        keyword_to_regex,
        match_keyword,
        scan_lessons,
        score_lessons,
    )

    # Basic smoke-test: functions are callable
    assert callable(scan_lessons)
    assert callable(score_lessons)
    assert callable(match_keyword)
    assert callable(keyword_to_regex)
    assert callable(filter_by_session_category)


def test_lazy_classes_in_all() -> None:
    """Indexer and ContextAssembler must remain in __all__ despite lazy import."""
    import gptme_rag

    assert "Indexer" in gptme_rag.__all__
    assert "ContextAssembler" in gptme_rag.__all__


def test_lazy_indexer_resolves() -> None:
    """gptme_rag.Indexer must resolve to the real class via __getattr__."""
    import gptme_rag

    cls = gptme_rag.Indexer
    assert cls.__name__ == "Indexer"


def test_unknown_attr_raises() -> None:
    """Unknown attributes must raise AttributeError, not silently return None."""
    import gptme_rag
    import pytest

    with pytest.raises(AttributeError, match="gptme_rag"):
        _ = gptme_rag.NonExistentClass
