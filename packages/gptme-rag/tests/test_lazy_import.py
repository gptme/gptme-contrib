"""Test that gptme_rag is importable without heavy deps (chromadb/sentence_transformers).

Phase 3 prerequisite: match-lessons.py and similar hooks must be able to
``import gptme_rag`` in the brain runtime without pulling in chromadb +
torch (which adds several GB and ~10s to every prompt-submit hook invocation).
"""

from __future__ import annotations

import sys


def test_plain_import_does_not_pull_chromadb() -> None:
    """``import gptme_rag`` must not import chromadb or sentence_transformers."""
    _HEAVY = ("chromadb", "sentence_transformers")

    # Snapshot and evict all gptme_rag submodules AND heavy deps so this test
    # measures what a fresh process would see, not what prior tests already loaded.
    saved: dict[str, object] = {}
    for k in list(sys.modules):
        if k.startswith("gptme_rag") or any(k == h or k.startswith(h + ".") for h in _HEAVY):
            saved[k] = sys.modules.pop(k)

    try:
        import gptme_rag  # noqa: F401

        heavy = [m for m in sys.modules if any(m == h or m.startswith(h + ".") for h in _HEAVY)]
        assert not heavy, f"Heavy deps imported after plain import: {heavy}"
    finally:
        # Restore original module state so later tests are not affected by
        # the transient eviction above.
        for k in list(sys.modules):
            if k.startswith("gptme_rag") or any(k == h or k.startswith(h + ".") for h in _HEAVY):
                del sys.modules[k]
        sys.modules.update(saved)


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


def test_missing_dep_raises_attribute_error_not_import_error() -> None:
    """When a heavy dep is absent, __getattr__ must raise AttributeError, not ImportError.

    hasattr() and getattr(..., default) only catch AttributeError.  If __getattr__
    propagates ImportError, code that probes for optional Indexer will crash
    instead of gracefully falling back.
    """
    import sys
    from unittest.mock import patch

    import gptme_rag
    import pytest

    # Block only the indexer submodule (None → ImportError).  Do NOT evict
    # gptme_rag.indexing.* broadly — that clobbers gptme_rag.indexing.document,
    # causing Document class-identity mismatches and pickling failures in sibling
    # tests that share Document instances across the session.
    with patch.dict(sys.modules, {"gptme_rag.indexing.indexer": None}):
        # hasattr must return False, not bubble up an ImportError.
        assert not hasattr(gptme_rag, "Indexer")
        # getattr with a default must return the default.
        sentinel = object()
        assert getattr(gptme_rag, "Indexer", sentinel) is sentinel
        # Direct access must raise AttributeError (not ImportError).
        with pytest.raises(AttributeError):
            _ = gptme_rag.Indexer


def test_dir_includes_lazy_names() -> None:
    """Lazy names (Indexer, ContextAssembler) must appear in dir(gptme_rag)."""
    import gptme_rag

    d = dir(gptme_rag)
    assert "Indexer" in d
    assert "ContextAssembler" in d
