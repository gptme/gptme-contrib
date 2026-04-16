"""Tests for gptme_sessions.discovery — session directory scanning."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gptme_sessions.discovery import (
    _quick_date_from_jsonl,
    _session_in_range,
    decode_cc_project_path,
    discover_cc_sessions,
    discover_codex_sessions,
    discover_copilot_sessions,
    discover_gptme_sessions,
    extract_cc_model,
    extract_project,
    extract_session_name,
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


def test_quick_date_from_jsonl_non_dict_lines(tmp_path: Path) -> None:
    """_quick_date_from_jsonl skips non-dict JSON values without crashing."""
    jsonl = tmp_path / "non_dict.jsonl"
    jsonl.write_text(
        '["list", "value"]\n'
        + "42\n"
        + '"string"\n'
        + "null\n"
        + json.dumps({"timestamp": "2026-03-05T10:00:00Z"})
        + "\n"
    )
    assert _quick_date_from_jsonl(jsonl) == date(2026, 3, 5)


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


# --- extract_cc_model ---


def test_extract_cc_model_finds_model(tmp_path: Path) -> None:
    """extract_cc_model returns model from first CC assistant message."""
    jsonl_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"message": {"role": "user", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "model": "claude-sonnet-4-6", "content": []}}),
    ]
    jsonl_file.write_text("\n".join(lines) + "\n")
    assert extract_cc_model(jsonl_file) == "claude-sonnet-4-6"


def test_extract_cc_model_no_assistant_message(tmp_path: Path) -> None:
    """extract_cc_model returns None when no assistant message is present."""
    jsonl_file = tmp_path / "session.jsonl"
    jsonl_file.write_text(json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n")
    assert extract_cc_model(jsonl_file) is None


def test_extract_cc_model_empty_file(tmp_path: Path) -> None:
    """extract_cc_model returns None for an empty file."""
    jsonl_file = tmp_path / "session.jsonl"
    jsonl_file.touch()
    assert extract_cc_model(jsonl_file) is None


def test_extract_cc_model_non_utf8_file(tmp_path: Path) -> None:
    """extract_cc_model returns None for a non-UTF-8 file (no crash)."""
    jsonl_file = tmp_path / "session.jsonl"
    jsonl_file.write_bytes(b"\xff\xfe invalid utf-8\n")
    assert extract_cc_model(jsonl_file) is None


def test_extract_cc_model_non_dict_lines(tmp_path: Path) -> None:
    """extract_cc_model skips non-dict JSON values without crashing."""
    jsonl_file = tmp_path / "session.jsonl"
    jsonl_file.write_text(
        '["list", "value"]\n'
        + "42\n"
        + '"string"\n'
        + "null\n"
        + json.dumps({"message": {"role": "assistant", "model": "claude-opus-4-6", "content": []}})
        + "\n"
    )
    assert extract_cc_model(jsonl_file) == "claude-opus-4-6"


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


def test_discover_gptme_sessions_excludes_evals(tmp_path: Path) -> None:
    """Eval benchmark sessions (gptme-evals-*) are excluded from discovery."""
    # Real session
    (tmp_path / "2026-03-05-dancing-blue-fish").mkdir()
    # Eval sessions — should be excluded
    (tmp_path / "2026-03-05-gptme-evals-anthropic--claude-sonnet-4-6-tool-abc123").mkdir()
    (
        tmp_path / "2026-03-05-gptme-evals-openrouter--anthropic--claude-haiku-4-5-tool-def456"
    ).mkdir()

    result = discover_gptme_sessions(date(2026, 3, 5), date(2026, 3, 5), logs_dir=tmp_path)
    assert len(result) == 1
    assert result[0].name == "2026-03-05-dancing-blue-fish"


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

    result = discover_cc_sessions(date(2026, 3, 5), date(2026, 3, 5), cc_dir=tmp_path, min_size=0)
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

    result = discover_cc_sessions(date(2026, 3, 5), date(2026, 3, 5), cc_dir=tmp_path, min_size=0)
    assert len(result) == 2


def test_discover_cc_sessions_nonexistent(tmp_path: Path) -> None:
    result = discover_cc_sessions(
        date(2026, 3, 5), date(2026, 3, 5), cc_dir=tmp_path / "nonexistent"
    )
    assert result == []


def test_discover_cc_sessions_filters_stubs(tmp_path: Path) -> None:
    """Stub sessions (<4KB) are excluded by default; real sessions are kept."""
    from gptme_sessions.discovery import CC_MIN_SESSION_SIZE

    project = tmp_path / "-home-bob-bob"
    project.mkdir()

    # Create a stub session (small file, <4KB) — should be filtered
    stub = _make_cc_session(project, "stub-session", "2026-03-05T10:00:00Z")
    assert stub.stat().st_size < CC_MIN_SESSION_SIZE  # sanity check

    # Create a real session (padded above threshold)
    real = project / "real-session.jsonl"
    line = json.dumps(
        {"type": "user", "timestamp": "2026-03-05T12:00:00Z", "message": {"content": "hi"}}
    )
    # Pad with enough assistant lines to exceed 4KB
    padding_line = json.dumps(
        {
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "x" * 200}],
            }
        }
    )
    real.write_text((line + "\n") + (padding_line + "\n") * 20)
    assert real.stat().st_size >= CC_MIN_SESSION_SIZE  # sanity check

    # Default min_size: stub excluded, real included
    result = discover_cc_sessions(date(2026, 3, 5), date(2026, 3, 5), cc_dir=tmp_path)
    assert len(result) == 1
    assert result[0].stem == "real-session"

    # With min_size=0: both included
    result_all = discover_cc_sessions(
        date(2026, 3, 5), date(2026, 3, 5), cc_dir=tmp_path, min_size=0
    )
    assert len(result_all) == 2


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


# --- extract_session_name ---


class TestExtractSessionName:
    def test_gptme_strips_date_prefix(self, tmp_path: Path) -> None:
        """gptme: strips YYYY-MM-DD- prefix from dir name."""
        session_dir = tmp_path / "2026-03-05-dancing-blue-fish"
        session_dir.mkdir()
        assert extract_session_name("gptme", session_dir) == "dancing-blue-fish"

    def test_gptme_jsonl_inside_dir(self, tmp_path: Path) -> None:
        """gptme: works with conversation.jsonl path (uses parent dir name)."""
        session_dir = tmp_path / "2026-03-05-my-session"
        session_dir.mkdir()
        jsonl = session_dir / "conversation.jsonl"
        jsonl.touch()
        assert extract_session_name("gptme", jsonl) == "my-session"

    def test_gptme_short_name(self, tmp_path: Path) -> None:
        """gptme: returns full name if no date prefix."""
        session_dir = tmp_path / "test-session"
        session_dir.mkdir()
        assert extract_session_name("gptme", session_dir) == "test-session"

    def test_cc_uses_first_8_chars(self, tmp_path: Path) -> None:
        """claude-code: uses first 8 chars of JSONL filename."""
        jsonl = tmp_path / "abc12345-def6-7890-abcd-ef1234567890.jsonl"
        jsonl.touch()
        assert extract_session_name("claude-code", jsonl) == "abc12345"

    def test_codex_uses_stem(self, tmp_path: Path) -> None:
        """codex: uses first 8 chars of JSONL stem."""
        jsonl = tmp_path / "session-rollout-123.jsonl"
        jsonl.touch()
        assert extract_session_name("codex", jsonl) == "session-"

    def test_copilot_uses_parent_dir(self, tmp_path: Path) -> None:
        """copilot: uses first 8 chars of parent dir name."""
        session_dir = tmp_path / "abcdefgh-1234-5678"
        session_dir.mkdir()
        events = session_dir / "events.jsonl"
        events.touch()
        assert extract_session_name("copilot", events) == "abcdefgh"


# --- extract_project ---


class TestExtractProject:
    def test_cc_decodes_project_dir(self, tmp_path: Path) -> None:
        """claude-code: decodes project dir name to filesystem path."""
        project_dir = tmp_path / "-Users-erb-myproject"
        project_dir.mkdir()
        jsonl = project_dir / "session.jsonl"
        jsonl.touch()
        assert extract_project("claude-code", jsonl) == "/Users/erb/myproject"

    def test_gptme_reads_workspace(self, tmp_path: Path) -> None:
        """gptme: reads workspace from config.toml."""
        session_dir = tmp_path / "2026-03-05-session"
        session_dir.mkdir()
        config = session_dir / "config.toml"
        config.write_text('[chat]\nworkspace = "/home/bob/gptme"\n')
        assert extract_project("gptme", session_dir) == "/home/bob/gptme"

    def test_gptme_jsonl_path(self, tmp_path: Path) -> None:
        """gptme: works with conversation.jsonl path."""
        session_dir = tmp_path / "2026-03-05-session"
        session_dir.mkdir()
        config = session_dir / "config.toml"
        config.write_text('[chat]\nworkspace = "/home/bob/gptme"\n')
        jsonl = session_dir / "conversation.jsonl"
        jsonl.touch()
        assert extract_project("gptme", jsonl) == "/home/bob/gptme"

    def test_gptme_no_config(self, tmp_path: Path) -> None:
        """gptme: returns None when config.toml is missing."""
        session_dir = tmp_path / "2026-03-05-session"
        session_dir.mkdir()
        assert extract_project("gptme", session_dir) is None

    def test_gptme_empty_workspace(self, tmp_path: Path) -> None:
        """gptme: returns None when workspace is empty."""
        session_dir = tmp_path / "2026-03-05-session"
        session_dir.mkdir()
        config = session_dir / "config.toml"
        config.write_text("[chat]\nmodel = 'opus'\n")
        assert extract_project("gptme", session_dir) is None

    def test_codex_returns_none(self, tmp_path: Path) -> None:
        """codex: returns None (no project info available)."""
        jsonl = tmp_path / "session.jsonl"
        jsonl.touch()
        assert extract_project("codex", jsonl) is None

    def test_copilot_returns_none(self, tmp_path: Path) -> None:
        """copilot: returns None (no project info available)."""
        events = tmp_path / "events.jsonl"
        events.touch()
        assert extract_project("copilot", events) is None
