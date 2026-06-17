"""Tests for the lesson evolution tracking module."""

import pytest
from gptme_lessons_extras.evolution import (
    EvolutionTracker,
    LessonHistory,
    LessonVersion,
    RefinementSuggestion,
)

# --- LessonVersion ---


def test_lesson_version_to_dict():
    v = LessonVersion(
        version=1,
        timestamp="2026-06-01T00:00:00",
        contributor="bob",
        changes="Initial creation",
        content_hash="abc12345",
    )
    d = v.to_dict()
    assert d["version"] == 1
    assert d["contributor"] == "bob"
    assert d["changes"] == "Initial creation"
    assert d["content_hash"] == "abc12345"


def test_lesson_version_roundtrip():
    v = LessonVersion(
        version=2,
        timestamp="2026-06-02T10:00:00",
        contributor="alice",
        changes="Added examples",
        content_hash="def67890",
    )
    d = v.to_dict()
    v2 = LessonVersion.from_dict(d)
    assert v2.version == v.version
    assert v2.contributor == v.contributor
    assert v2.changes == v.changes
    assert v2.content_hash == v.content_hash


# --- LessonHistory ---


def _make_history() -> LessonHistory:
    return LessonHistory(
        lesson_id="tools/some-lesson.md",
        origin_agent="bob",
        created="2026-06-01T00:00:00",
        versions=[
            LessonVersion(
                version=1,
                timestamp="2026-06-01T00:00:00",
                contributor="bob",
                changes="Initial creation",
                content_hash="aaa",
            ),
            LessonVersion(
                version=2,
                timestamp="2026-06-02T00:00:00",
                contributor="alice",
                changes="Clarified examples",
                content_hash="bbb",
            ),
        ],
    )


def test_lesson_history_to_dict():
    h = _make_history()
    d = h.to_dict()
    assert d["lesson_id"] == "tools/some-lesson.md"
    assert d["origin_agent"] == "bob"
    assert len(d["versions"]) == 2


def test_lesson_history_roundtrip():
    h = _make_history()
    d = h.to_dict()
    h2 = LessonHistory.from_dict(d)
    assert h2.lesson_id == h.lesson_id
    assert h2.origin_agent == h.origin_agent
    assert len(h2.versions) == len(h.versions)
    assert h2.versions[0].version == 1
    assert h2.versions[1].contributor == "alice"


def test_lesson_history_latest_version():
    h = _make_history()
    latest = h.latest_version()
    assert latest is not None
    assert latest.version == 2


def test_lesson_history_latest_version_empty():
    h = LessonHistory(lesson_id="x", origin_agent="bob", created="2026-01-01")
    assert h.latest_version() is None


def test_lesson_history_get_version():
    h = _make_history()
    v1 = h.get_version(1)
    assert v1 is not None
    assert v1.changes == "Initial creation"

    v2 = h.get_version(2)
    assert v2 is not None
    assert v2.contributor == "alice"


def test_lesson_history_get_version_missing():
    h = _make_history()
    assert h.get_version(99) is None


def test_lesson_history_contributors():
    h = _make_history()
    contributors = h.contributors()
    assert "bob" in contributors
    assert "alice" in contributors


def test_lesson_history_contributors_unique():
    h = LessonHistory(
        lesson_id="x",
        origin_agent="bob",
        created="2026-01-01",
        versions=[
            LessonVersion(
                version=1,
                timestamp="t",
                contributor="bob",
                changes="init",
                content_hash="h",
            ),
            LessonVersion(
                version=2,
                timestamp="t",
                contributor="bob",
                changes="fix",
                content_hash="h2",
            ),
        ],
    )
    assert h.contributors().count("bob") == 1


# --- RefinementSuggestion ---


def test_refinement_suggestion_roundtrip():
    r = RefinementSuggestion(
        lesson_id="tools/x.md",
        suggester="alice",
        timestamp="2026-06-01T00:00:00",
        category="clarity",
        suggestion="Add an example",
        priority="medium",
        status="proposed",
    )
    d = r.to_dict()
    r2 = RefinementSuggestion.from_dict(d)
    assert r2.lesson_id == r.lesson_id
    assert r2.suggester == r.suggester
    assert r2.category == r.category
    assert r2.status == r.status


# --- EvolutionTracker ---


def test_evolution_tracker_initialize_lesson(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    history = tracker.initialize_lesson(
        lesson_id="tools/some-lesson.md",
        origin_agent="bob",
        content="# Some lesson\n\nContent here.",
    )
    assert history.lesson_id == "tools/some-lesson.md"
    assert history.origin_agent == "bob"
    assert len(history.versions) == 1
    assert history.versions[0].version == 1
    assert history.versions[0].changes == "Initial creation"


def test_evolution_tracker_persist_and_load(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    tracker.initialize_lesson("tools/x.md", "bob", "content")

    loaded = tracker.load_history("tools/x.md")
    assert loaded is not None
    assert loaded.lesson_id == "tools/x.md"
    assert loaded.origin_agent == "bob"


def test_evolution_tracker_load_missing(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    assert tracker.load_history("does-not-exist.md") is None


def test_evolution_tracker_track_change(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    tracker.initialize_lesson("tools/x.md", "bob", "v1 content")

    new_version = tracker.track_change(
        lesson_id="tools/x.md",
        contributor="alice",
        changes="Improved examples",
        content="v2 content",
    )
    assert new_version.version == 2
    assert new_version.contributor == "alice"
    assert new_version.changes == "Improved examples"

    loaded = tracker.load_history("tools/x.md")
    assert loaded is not None
    assert len(loaded.versions) == 2
    assert loaded.latest_version().version == 2  # type: ignore[union-attr]


def test_evolution_tracker_track_change_uninitialized(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    with pytest.raises(ValueError, match="No history found"):
        tracker.track_change("tools/x.md", "bob", "changes", "content")


def test_evolution_tracker_content_hash_differs(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    tracker.initialize_lesson("tools/x.md", "bob", "content v1")
    v2 = tracker.track_change("tools/x.md", "alice", "update", "content v2")

    loaded = tracker.load_history("tools/x.md")
    assert loaded is not None
    v1_hash = loaded.versions[0].content_hash
    assert v1_hash != v2.content_hash


def test_evolution_tracker_suggest_refinement(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    tracker.initialize_lesson("tools/x.md", "bob", "content")

    ref = tracker.suggest_refinement(
        lesson_id="tools/x.md",
        suggester="alice",
        category="clarity",
        suggestion="Add a concrete example",
        priority="high",
    )
    assert ref.lesson_id == "tools/x.md"
    assert ref.status == "proposed"
    assert ref.priority == "high"


def test_evolution_tracker_load_refinements(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    tracker.initialize_lesson("tools/x.md", "bob", "content")
    tracker.suggest_refinement(
        "tools/x.md", "alice", "clarity", "Add example", "medium"
    )
    tracker.suggest_refinement("tools/x.md", "bob", "pattern", "Add pattern", "low")

    refs = tracker.load_refinements("tools/x.md")
    assert len(refs) == 2
    assert refs[0].category == "clarity"
    assert refs[1].category == "pattern"


def test_evolution_tracker_update_refinement_status(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    tracker.initialize_lesson("tools/x.md", "bob", "content")
    tracker.suggest_refinement("tools/x.md", "alice", "clarity", "suggestion", "medium")

    tracker.update_refinement_status("tools/x.md", 0, "accepted")

    refs = tracker.load_refinements("tools/x.md")
    assert refs[0].status == "accepted"


def test_evolution_tracker_update_refinement_invalid_index(tmp_path):
    tracker = EvolutionTracker(history_dir=tmp_path / "history")
    tracker.initialize_lesson("tools/x.md", "bob", "content")
    tracker.suggest_refinement("tools/x.md", "alice", "clarity", "suggestion", "medium")

    with pytest.raises(ValueError, match="Invalid suggestion index"):
        tracker.update_refinement_status("tools/x.md", 99, "accepted")
