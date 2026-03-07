"""Tests for gptme_sessions.discovery — session directory scanning."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gptme_sessions.discovery import (
    _duration_from_timestamps,
    _gptme_session_to_record,
    _cc_session_to_record,
    _quick_date_from_jsonl,
    _quick_first_last_ts,
    _session_in_range,
    decode_cc_project_path,
    discover_all,
    discover_cc_sessions,
    discover_codex_sessions,
    discover_copilot_sessions,
    discover_gptme_sessions,
    parse_gptme_config,
)


# --- _session_in_range ---


@pytest.mark.parametrize(
    "name,start,end,expected",
    [
        ("2026-03-05-hello", date(2026, 3, 5), date(2026, 3, 5), True),
        ("2026-03-05-hello", date(2026, 3, 1), date(2026, 3, 31), True),
        ("2026-03-05-hello", date(2026, 3, 6), date(2026, 3, 10), False),
        ("2026-03-05-hello", date(2026, 3, 1), date(2026, 3, 4), False),
        ("not-a-date", date(2026, 3, 1), date(2026, 3, 31), False),
        ("short", date(2026, 3, 1), date(2026, 3, 31), False),
    ],
)
def test_session_in_range(name: str, start: date, end: date, expected: bool) -> None:
    assert _session_in_range(name, start, end) == expected


# --- decode_cc_project_path ---


@pytest.mark.parametrize(
    "encoded,expected",
    [
        ("-home-bob-bob", "/home/bob/bob"),
        ("-Users-erb-Programming-gptme", "/Users/erb/Programming/gptme"),
        ("not-encoded", "not-encoded"),
    ],
)
def test_decode_cc_project_path(encoded: str, expected: str) -> None:
    assert decode_cc_project_path(encoded) == expected


# --- _quick_date_from_jsonl ---


def test_quick_date_from_jsonl(tmp_path: Path) -> None:
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "timestamp": "2026-03-05T10:00:00Z"})
        + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-03-05T10:05:00Z"})
        + "\n"
    )
    assert _quick_date_from_jsonl(jsonl) == date(2026, 3, 5)


def test_quick_date_from_jsonl_empty(tmp_path: Path) -> None:
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    assert _quick_date_from_jsonl(jsonl) is None


def test_quick_date_from_jsonl_no_timestamp(tmp_path: Path) -> None:
    jsonl = tmp_path / "no_ts.jsonl"
    jsonl.write_text(json.dumps({"type": "user", "content": "hello"}) + "\n")
    assert _quick_date_from_jsonl(jsonl) is None


# --- parse_gptme_config ---


def test_parse_gptme_config_full(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[chat]\nmodel = "anthropic/claude-sonnet-4-20250514"\nworkspace = "/home/bob/gptme"\ninteractive = false\n'
    )
    result = parse_gptme_config(tmp_path)
    assert result["model"] == "anthropic/claude-sonnet-4-20250514"
    assert result["workspace"] == "/home/bob/gptme"
    assert result["interactive"] is False


def test_parse_gptme_config_missing(tmp_path: Path) -> None:
    result = parse_gptme_config(tmp_path)
    assert result == {"model": "", "workspace": "", "interactive": True}


def test_parse_gptme_config_minimal(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[chat]\nmodel = "openai/gpt-4o"\n')
    result = parse_gptme_config(tmp_path)
    assert result["model"] == "openai/gpt-4o"
    assert result["workspace"] == ""
    assert result["interactive"] is True


# --- discover_gptme_sessions ---


def test_discover_gptme_sessions(tmp_path: Path) -> None:
    """Test scanning gptme session dirs by date range."""
    # Create session dirs
    (tmp_path / "2026-03-04-session-a").mkdir()
    (tmp_path / "2026-03-05-session-b").mkdir()
    (tmp_path / "2026-03-05-session-c").mkdir()
    (tmp_path / "2026-03-06-session-d").mkdir()
    # Create a non-dir file (should be skipped)
    (tmp_path / "2026-03-05-file.txt").write_text("not a dir")

    result = discover_gptme_sessions(date(2026, 3, 5), date(2026, 3, 5), logs_dir=tmp_path)
    assert len(result) == 2
    assert all(p.is_dir() for p in result)
    assert result[0].name == "2026-03-05-session-b"
    assert result[1].name == "2026-03-05-session-c"


def test_discover_gptme_sessions_range(tmp_path: Path) -> None:
    (tmp_path / "2026-03-03-old").mkdir()
    (tmp_path / "2026-03-04-start").mkdir()
    (tmp_path / "2026-03-05-mid").mkdir()
    (tmp_path / "2026-03-06-end").mkdir()
    (tmp_path / "2026-03-07-future").mkdir()

    result = discover_gptme_sessions(date(2026, 3, 4), date(2026, 3, 6), logs_dir=tmp_path)
    assert len(result) == 3
    names = [p.name for p in result]
    assert "2026-03-04-start" in names
    assert "2026-03-05-mid" in names
    assert "2026-03-06-end" in names


def test_discover_gptme_sessions_empty(tmp_path: Path) -> None:
    result = discover_gptme_sessions(date(2026, 3, 5), date(2026, 3, 5), logs_dir=tmp_path)
    assert result == []


def test_discover_gptme_sessions_nonexistent(tmp_path: Path) -> None:
    result = discover_gptme_sessions(
        date(2026, 3, 5), date(2026, 3, 5), logs_dir=tmp_path / "nonexistent"
    )
    assert result == []


# --- discover_cc_sessions ---


def _make_cc_session(project_dir: Path, name: str, ts: str) -> Path:
    """Helper to create a minimal CC session JSONL file."""
    jsonl = project_dir / f"{name}.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "timestamp": ts, "message": {"content": "hi"}}) + "\n"
    )
    return jsonl


def test_discover_cc_sessions(tmp_path: Path) -> None:
    """Test scanning CC session files by date range."""
    project = tmp_path / "-home-bob-bob"
    project.mkdir()

    _make_cc_session(project, "session1", "2026-03-04T10:00:00Z")
    _make_cc_session(project, "session2", "2026-03-05T12:00:00Z")
    _make_cc_session(project, "session3", "2026-03-05T14:00:00Z")
    _make_cc_session(project, "session4", "2026-03-06T09:00:00Z")

    result = discover_cc_sessions(date(2026, 3, 5), date(2026, 3, 5), cc_dir=tmp_path)
    assert len(result) == 2
    names = [p.stem for p in result]
    assert "session2" in names
    assert "session3" in names


def test_discover_cc_sessions_multi_project(tmp_path: Path) -> None:
    """Test scanning across multiple CC project directories."""
    proj_a = tmp_path / "-home-bob-proj-a"
    proj_a.mkdir()
    proj_b = tmp_path / "-home-bob-proj-b"
    proj_b.mkdir()

    _make_cc_session(proj_a, "s1", "2026-03-05T10:00:00Z")
    _make_cc_session(proj_b, "s2", "2026-03-05T11:00:00Z")
    _make_cc_session(proj_b, "s3", "2026-03-04T11:00:00Z")  # out of range

    result = discover_cc_sessions(date(2026, 3, 5), date(2026, 3, 5), cc_dir=tmp_path)
    assert len(result) == 2


def test_discover_cc_sessions_nonexistent(tmp_path: Path) -> None:
    result = discover_cc_sessions(
        date(2026, 3, 5), date(2026, 3, 5), cc_dir=tmp_path / "nonexistent"
    )
    assert result == []


# --- discover_codex_sessions ---


def _make_codex_session(day_dir: Path, name: str) -> Path:
    """Helper to create a minimal Codex session JSONL file."""
    jsonl = day_dir / f"{name}.jsonl"
    jsonl.write_text(
        json.dumps({"type": "session_meta", "payload": {"originator": "codex_exec"}}) + "\n"
    )
    return jsonl


def test_discover_codex_sessions(tmp_path: Path) -> None:
    """Test scanning Codex sessions by YYYY/MM/DD directory structure."""
    day_in = tmp_path / "2026" / "03" / "05"
    day_in.mkdir(parents=True)
    day_out = tmp_path / "2026" / "03" / "04"
    day_out.mkdir(parents=True)

    s1 = _make_codex_session(day_in, "session1")
    s2 = _make_codex_session(day_in, "session2")
    _make_codex_session(day_out, "old-session")

    result = discover_codex_sessions(date(2026, 3, 5), date(2026, 3, 5), codex_dir=tmp_path)
    assert len(result) == 2
    assert s1 in result
    assert s2 in result


def test_discover_codex_sessions_nonexistent(tmp_path: Path) -> None:
    result = discover_codex_sessions(
        date(2026, 3, 5), date(2026, 3, 5), codex_dir=tmp_path / "nonexistent"
    )
    assert result == []


def test_discover_codex_sessions_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CODEX_SESSIONS_DIR env var overrides the default path."""
    day = tmp_path / "2026" / "03" / "05"
    day.mkdir(parents=True)
    _make_codex_session(day, "env-session")

    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path))
    # No explicit codex_dir — should pick up env var
    result = discover_codex_sessions(date(2026, 3, 5), date(2026, 3, 5))
    assert len(result) == 1
    assert result[0].name == "env-session.jsonl"


# --- discover_copilot_sessions ---


def _make_copilot_session(state_dir: Path, uuid: str, ts: str) -> Path:
    """Helper to create a minimal Copilot session events.jsonl file."""
    session_dir = state_dir / uuid
    session_dir.mkdir(parents=True)
    events_file = session_dir / "events.jsonl"
    events_file.write_text(
        json.dumps(
            {
                "type": "session.start",
                "timestamp": ts,
                "data": {"producer": "copilot-agent"},
            }
        )
        + "\n"
    )
    return events_file


def test_discover_copilot_sessions(tmp_path: Path) -> None:
    """Test scanning Copilot sessions by timestamp in events.jsonl."""
    _make_copilot_session(tmp_path, "uuid-1", "2026-03-05T10:00:00Z")
    _make_copilot_session(tmp_path, "uuid-2", "2026-03-05T14:00:00Z")
    _make_copilot_session(tmp_path, "uuid-3", "2026-03-04T10:00:00Z")  # out of range

    result = discover_copilot_sessions(date(2026, 3, 5), date(2026, 3, 5), copilot_dir=tmp_path)
    assert len(result) == 2
    uuids = {p.parent.name for p in result}
    assert "uuid-1" in uuids
    assert "uuid-2" in uuids


def test_discover_copilot_sessions_nonexistent(tmp_path: Path) -> None:
    result = discover_copilot_sessions(
        date(2026, 3, 5), date(2026, 3, 5), copilot_dir=tmp_path / "nonexistent"
    )
    assert result == []


def test_discover_copilot_sessions_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """COPILOT_STATE_DIR env var overrides the default path."""
    _make_copilot_session(tmp_path, "env-uuid", "2026-03-05T09:00:00Z")

    monkeypatch.setenv("COPILOT_STATE_DIR", str(tmp_path))
    # No explicit copilot_dir — should pick up env var
    result = discover_copilot_sessions(date(2026, 3, 5), date(2026, 3, 5))
    assert len(result) == 1
    assert result[0].parent.name == "env-uuid"


def test_discover_copilot_sessions_sorted_by_date(tmp_path: Path) -> None:
    """Results are sorted by session date, not by UUID directory name."""
    # UUID "zzz" has an earlier date than "aaa" — alphabetical sort would give wrong order
    _make_copilot_session(tmp_path, "zzz-early", "2026-03-04T08:00:00Z")
    _make_copilot_session(tmp_path, "aaa-late", "2026-03-05T20:00:00Z")

    result = discover_copilot_sessions(date(2026, 3, 4), date(2026, 3, 5), copilot_dir=tmp_path)
    assert len(result) == 2
    # Should be sorted by date: early session first, late session second
    assert result[0].parent.name == "zzz-early"
    assert result[1].parent.name == "aaa-late"


# --- _quick_first_last_ts ---


def test_quick_first_last_ts(tmp_path: Path) -> None:
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "timestamp": "2026-03-05T10:00:00Z"})
        + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-03-05T10:30:00Z"})
        + "\n"
    )
    first, last = _quick_first_last_ts(jsonl)
    assert first == "2026-03-05T10:00:00Z"
    assert last == "2026-03-05T10:30:00Z"


def test_quick_first_last_ts_empty(tmp_path: Path) -> None:
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    first, last = _quick_first_last_ts(jsonl)
    assert first is None
    assert last is None


# --- _duration_from_timestamps ---


def test_duration_from_timestamps() -> None:
    assert _duration_from_timestamps("2026-03-05T10:00:00Z", "2026-03-05T10:30:00Z") == 1800


def test_duration_from_timestamps_none() -> None:
    assert _duration_from_timestamps(None, "2026-03-05T10:30:00Z") == 0
    assert _duration_from_timestamps("2026-03-05T10:00:00Z", None) == 0


# --- _gptme_session_to_record ---


def test_gptme_session_to_record(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-03-05-test-session"
    session_dir.mkdir()
    # Write a config.toml
    (session_dir / "config.toml").write_text(
        '[chat]\nmodel = "anthropic/claude-sonnet-4-20250514"\ninteractive = false\n'
    )
    # Write a conversation.jsonl
    (session_dir / "conversation.jsonl").write_text(
        json.dumps({"role": "user", "timestamp": "2026-03-05T10:00:00Z"})
        + "\n"
        + json.dumps({"role": "assistant", "timestamp": "2026-03-05T10:25:00Z"})
        + "\n"
    )
    record = _gptme_session_to_record(session_dir)
    assert record.harness == "gptme"
    assert record.model == "anthropic/claude-sonnet-4-20250514"
    assert record.model_normalized == "sonnet"
    assert record.run_type == "autonomous"
    assert record.duration_seconds == 1500  # 25 minutes
    assert record.session_id == "test-session"


def test_gptme_session_to_record_interactive(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-03-05-interactive"
    session_dir.mkdir()
    (session_dir / "config.toml").write_text('[chat]\nmodel = "openai/gpt-4o"\n')
    record = _gptme_session_to_record(session_dir)
    assert record.run_type == "interactive"
    assert record.duration_seconds == 0  # no conversation.jsonl


# --- _cc_session_to_record ---


def test_cc_session_to_record(tmp_path: Path) -> None:
    project_dir = tmp_path / "-home-bob-bob"
    project_dir.mkdir()
    jsonl = project_dir / "abc123.jsonl"
    jsonl.write_text(
        json.dumps({"type": "user", "timestamp": "2026-03-05T14:00:00Z"})
        + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-03-05T14:45:00Z"})
        + "\n"
    )
    record = _cc_session_to_record(jsonl)
    assert record.harness == "claude-code"
    assert record.session_id == "abc123"
    assert record.duration_seconds == 2700  # 45 minutes


# --- discover_all ---


def test_discover_all_gptme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_all finds gptme sessions and converts to records."""
    # Set up a fake gptme logs dir
    logs_dir = tmp_path / "gptme-logs"
    logs_dir.mkdir()
    session = logs_dir / "2026-03-05-my-session"
    session.mkdir()
    (session / "config.toml").write_text(
        '[chat]\nmodel = "anthropic/claude-opus-4-6"\ninteractive = false\n'
    )
    (session / "conversation.jsonl").write_text(
        json.dumps({"role": "user", "timestamp": "2026-03-05T10:00:00Z"}) + "\n"
    )

    monkeypatch.setenv("GPTME_LOGS_DIR", str(logs_dir))
    # Point other dirs to nonexistent paths to avoid picking up real sessions
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "no-cc"))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path / "no-codex"))
    monkeypatch.setenv("COPILOT_STATE_DIR", str(tmp_path / "no-copilot"))

    records = discover_all(since_days=7)
    assert len(records) == 1
    assert records[0].harness == "gptme"
    assert records[0].model_normalized == "opus"
    assert records[0].run_type == "autonomous"


def test_discover_all_mixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_all finds sessions across multiple harnesses."""
    # gptme
    gptme_dir = tmp_path / "gptme"
    gptme_dir.mkdir()
    session = gptme_dir / "2026-03-05-gptme-sess"
    session.mkdir()
    (session / "config.toml").write_text('[chat]\nmodel = "openai/gpt-4o"\ninteractive = false\n')

    # Claude Code
    cc_dir = tmp_path / "cc"
    project = cc_dir / "-home-bob-bob"
    project.mkdir(parents=True)
    (project / "cc-session.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-03-05T12:00:00Z"}) + "\n"
    )

    monkeypatch.setenv("GPTME_LOGS_DIR", str(gptme_dir))
    monkeypatch.setenv("CLAUDE_HOME", str(cc_dir.parent))  # parent since it appends /projects
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path / "no-codex"))
    monkeypatch.setenv("COPILOT_STATE_DIR", str(tmp_path / "no-copilot"))

    records = discover_all(since_days=7, cc_dir=cc_dir)
    assert len(records) == 2
    harnesses = {r.harness for r in records}
    assert "gptme" in harnesses
    assert "claude-code" in harnesses
