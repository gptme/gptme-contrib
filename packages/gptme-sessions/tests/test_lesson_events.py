"""Tests for lesson-event ingestion (producer: the match-lessons CC hook)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gptme_sessions.lesson_events import (
    MAX_LESSON_EVENTS,
    lesson_events_path,
    load_lesson_events,
)
from gptme_sessions.record import SessionRecord


def _write_events(tmp_path: Path, session_id: str, events: list[dict]) -> Path:
    path = tmp_path / f"cc-session-{session_id}-lessons.jsonl"
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in events),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def tmpdir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("CC_SESSION_ID", raising=False)
    return tmp_path


class TestLessonEventsPath:
    def test_uses_tmpdir_and_hook_naming(self, tmpdir_env):
        assert lesson_events_path("abc-123") == tmpdir_env / "cc-session-abc-123-lessons.jsonl"

    def test_sanitizes_session_id(self, tmpdir_env):
        assert lesson_events_path("a/b:c@d").name == "cc-session-a_b_c_d-lessons.jsonl"

    def test_matches_hook_implementation(self, tmpdir_env):
        """The hook can't import this package, so pin the shared contract here.

        If ``_get_lesson_events_file`` in match-lessons.py ever changes shape,
        this fails instead of silently producing files nothing reads.
        """
        hook = (
            Path(__file__).resolve().parents[3]
            / "scripts"
            / "claude-code-hooks"
            / "match-lessons.py"
        )
        assert hook.exists(), f"contract producer is missing: {hook}"
        source = hook.read_text(encoding="utf-8")
        assert 'f"cc-session-{safe_id}-lessons.jsonl"' in source
        assert re.search(r'safe_id = re\.sub\(r"\[\^a-zA-Z0-9_-\]", "_", session_id\)', source)


class TestLoadLessonEvents:
    def test_missing_file_returns_empty(self, tmpdir_env):
        assert load_lesson_events("nope") == []

    def test_no_session_id_returns_empty(self, tmpdir_env):
        assert load_lesson_events() == []

    def test_loads_events(self, tmpdir_env):
        events = [
            {"lesson_name": "A", "match_type": "keyword", "sequence_order": 1},
            {"lesson_name": "B", "match_type": "semantic", "sequence_order": 2},
        ]
        _write_events(tmpdir_env, "s1", events)
        assert load_lesson_events("s1") == events

    def test_falls_back_to_cc_session_id_env(self, tmpdir_env, monkeypatch):
        _write_events(tmpdir_env, "envid", [{"lesson_name": "A"}])
        monkeypatch.setenv("CC_SESSION_ID", "envid")
        assert load_lesson_events() == [{"lesson_name": "A"}]

    def test_skips_partial_trailing_line(self, tmpdir_env):
        path = _write_events(tmpdir_env, "s2", [{"lesson_name": "A"}])
        with path.open("a", encoding="utf-8") as f:
            f.write('{"lesson_name": "B", "match_ty')
        assert load_lesson_events("s2") == [{"lesson_name": "A"}]

    def test_skips_non_dict_entries(self, tmpdir_env):
        path = tmpdir_env / "cc-session-s3-lessons.jsonl"
        path.write_text('{"lesson_name": "A"}\n["nope"]\n\n', encoding="utf-8")
        assert load_lesson_events("s3") == [{"lesson_name": "A"}]

    def test_truncates_to_max_events(self, tmpdir_env):
        _write_events(tmpdir_env, "s4", [{"i": i} for i in range(10)])
        loaded = load_lesson_events("s4", max_events=3)
        assert loaded == [{"i": 0}, {"i": 1}, {"i": 2}]

    def test_default_cap_is_positive(self):
        assert MAX_LESSON_EVENTS > 0


class TestSessionRecordField:
    def test_defaults_to_empty_list(self):
        assert SessionRecord(session_id="x").lesson_events == []

    def test_json_null_coerced_to_empty_list(self):
        assert SessionRecord(session_id="x", lesson_events=None).lesson_events == []

    def test_round_trips_through_dict(self):
        events = [{"lesson_name": "A", "match_type": "keyword"}]
        record = SessionRecord(session_id="x", lesson_events=events)
        restored = SessionRecord.from_dict(record.to_dict())
        assert restored.lesson_events == events
