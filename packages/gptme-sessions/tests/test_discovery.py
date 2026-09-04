"""Tests for gptme_sessions.discovery — session directory scanning."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from gptme_sessions.discovery import (
    _expand_pi_path,
    _first_event_cwd,
    _quick_date_from_jsonl,
    _quick_datetime_from_jsonl,
    _session_in_range,
    decode_cc_project_path,
    discover_cc_sessions,
    discover_codex_sessions,
    discover_copilot_sessions,
    discover_gptme_sessions,
    discover_pi_sessions,
    extract_cc_model,
    extract_project,
    extract_session_name,
    find_cc_session_file,
    parse_gptme_config,
    resolve_cc_session_model,
    session_datetime_from_path,
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


# --- _quick_datetime_from_jsonl ---


def test_quick_datetime_from_jsonl_returns_full_datetime(tmp_path: Path) -> None:
    """Real start time is preserved — not collapsed to a date."""
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(json.dumps({"type": "user", "timestamp": "2026-03-05T22:42:48Z"}) + "\n")
    assert _quick_datetime_from_jsonl(jsonl) == datetime(
        2026, 3, 5, 22, 42, 48, tzinfo=timezone.utc
    )


def test_quick_datetime_from_jsonl_missing(tmp_path: Path) -> None:
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    assert _quick_datetime_from_jsonl(jsonl) is None


# --- session_datetime_from_path ---


def test_session_datetime_from_path_claude_code(tmp_path: Path) -> None:
    """Avoids the noon-UTC placeholder bug by returning the trajectory's real start time."""
    jsonl = tmp_path / "abc12345-ffff.jsonl"
    jsonl.write_text(json.dumps({"type": "system", "timestamp": "2026-04-15T22:42:48.123Z"}) + "\n")
    dt = session_datetime_from_path("claude-code", jsonl)
    assert dt is not None
    assert dt.date() == date(2026, 4, 15)
    assert (dt.hour, dt.minute, dt.second) == (22, 42, 48)


def test_session_datetime_from_path_missing_file_returns_none(tmp_path: Path) -> None:
    jsonl = tmp_path / "does-not-exist.jsonl"
    assert session_datetime_from_path("claude-code", jsonl) is None


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


def test_extract_cc_model_skips_synthetic_sentinel(tmp_path: Path) -> None:
    """extract_cc_model treats `<synthetic>` (CC's auth-failure sentinel) as no model
    and continues scanning for a real assistant model.
    """
    jsonl_file = tmp_path / "session.jsonl"
    lines = [
        # First assistant message is the synthetic 401 reply
        json.dumps({"message": {"role": "assistant", "model": "<synthetic>", "content": []}}),
        # Real model on the next assistant message
        json.dumps({"message": {"role": "assistant", "model": "claude-opus-4-7", "content": []}}),
    ]
    jsonl_file.write_text("\n".join(lines) + "\n")
    assert extract_cc_model(jsonl_file) == "claude-opus-4-7"


def test_extract_cc_model_synthetic_only_returns_none(tmp_path: Path) -> None:
    """All-synthetic trajectory (auth failed before any real call) returns None."""
    jsonl_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"message": {"role": "user", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "model": "<synthetic>", "content": []}}),
    ]
    jsonl_file.write_text("\n".join(lines) + "\n")
    assert extract_cc_model(jsonl_file) is None


def test_extract_cc_model_skips_unknown_sentinel(tmp_path: Path) -> None:
    """`unknown` is also a sentinel — skip it like `<synthetic>`."""
    jsonl_file = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"message": {"role": "assistant", "model": "unknown", "content": []}}),
        json.dumps({"message": {"role": "assistant", "model": "claude-sonnet-4-6", "content": []}}),
    ]
    jsonl_file.write_text("\n".join(lines) + "\n")
    assert extract_cc_model(jsonl_file) == "claude-sonnet-4-6"


# --- resolve_cc_session_model ---


def _write_stream_log(
    tmp_dir: Path,
    session_id: str,
    log_path: Path,
    first_line: dict | str,
) -> None:
    """Write a stream log and its pointer file for test setup."""
    if isinstance(first_line, dict):
        log_path.write_text(json.dumps(first_line) + "\n", encoding="utf-8")
    else:
        log_path.write_text(first_line, encoding="utf-8")
    (tmp_dir / f"cc-session-log-ref-{session_id}.txt").write_text(
        str(log_path) + "\n", encoding="utf-8"
    )


def test_resolve_cc_session_model_stream_log_synthetic_returns_none(tmp_path: Path) -> None:
    """Stream log whose init line is `<synthetic>` (auth failed before any real
    call) returns None — never pollutes the bandit with a sentinel arm."""
    sid = "synthetic-stream"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    log_path = tmp_dir / "cc-session-syn.log"
    _write_stream_log(
        tmp_dir,
        sid,
        log_path,
        {"type": "system", "subtype": "init", "session_id": sid, "model": "<synthetic>"},
    )
    assert resolve_cc_session_model(sid, tmp_dir=tmp_dir) is None


def test_resolve_cc_session_model_from_stream_log(tmp_path: Path) -> None:
    """Resolve uses the stream log when the pointer file exists — covers
    `claude -p --stream-json` autonomous sessions whose trajectory is a stub."""
    sid = "abcd-1234"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    log_path = tmp_dir / "cc-session-deadbeef.log"
    _write_stream_log(
        tmp_dir,
        sid,
        log_path,
        {
            "type": "system",
            "subtype": "init",
            "session_id": sid,
            "model": "claude-sonnet-4-6",
        },
    )
    assert resolve_cc_session_model(sid, tmp_dir=tmp_dir) == "claude-sonnet-4-6"


def test_resolve_cc_session_model_stream_log_preferred_over_stub(tmp_path: Path) -> None:
    """When both a stub trajectory (no assistant messages) and a stream log
    exist, the stream log wins — the stub can't attribute."""
    sid = "stub-and-log"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    log_path = tmp_dir / "cc-session-xyz.log"
    _write_stream_log(
        tmp_dir, sid, log_path, {"type": "system", "subtype": "init", "model": "claude-opus-4-6"}
    )
    # Stub trajectory — aiTitle only, no assistant message
    (project_dir / f"{sid}.jsonl").write_text(
        json.dumps({"aiTitle": "some title", "sessionId": sid, "type": "summary"}) + "\n",
        encoding="utf-8",
    )

    assert (
        resolve_cc_session_model(sid, project_dir=project_dir, tmp_dir=tmp_dir) == "claude-opus-4-6"
    )


def test_resolve_cc_session_model_falls_back_to_trajectory(tmp_path: Path) -> None:
    """When no pointer/log exists (interactive session), fall back to the
    trajectory file in the project dir."""
    sid = "interactive-7890"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    (project_dir / f"{sid}.jsonl").write_text(
        json.dumps({"message": {"role": "user", "content": "hi"}})
        + "\n"
        + json.dumps({"message": {"role": "assistant", "model": "claude-opus-4-7", "content": []}})
        + "\n",
        encoding="utf-8",
    )

    assert (
        resolve_cc_session_model(sid, project_dir=project_dir, tmp_dir=tmp_dir) == "claude-opus-4-7"
    )


def test_resolve_cc_session_model_returns_none_when_nothing_resolves(tmp_path: Path) -> None:
    """Neither a stream log nor a trajectory attributes — refuse to guess.
    Guards the ErikBjare/bob#615 bandit-contamination invariant."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    assert resolve_cc_session_model("ghost-id", project_dir=project_dir, tmp_dir=tmp_dir) is None


def test_resolve_cc_session_model_empty_session_id(tmp_path: Path) -> None:
    """Empty session id short-circuits to None without touching the filesystem."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    assert resolve_cc_session_model("", tmp_dir=tmp_dir) is None


def test_resolve_cc_session_model_dangling_pointer_falls_back(tmp_path: Path) -> None:
    """Pointer file exists but references a non-existent log — fall back to
    the trajectory rather than returning None."""
    sid = "dangling-pointer"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    (tmp_dir / f"cc-session-log-ref-{sid}.txt").write_text(
        "/tmp/does-not-exist.log\n", encoding="utf-8"
    )
    (project_dir / f"{sid}.jsonl").write_text(
        json.dumps({"message": {"role": "assistant", "model": "claude-haiku-4-5", "content": []}})
        + "\n",
        encoding="utf-8",
    )

    assert (
        resolve_cc_session_model(sid, project_dir=project_dir, tmp_dir=tmp_dir)
        == "claude-haiku-4-5"
    )


def test_resolve_cc_session_model_stream_log_no_model_field(tmp_path: Path) -> None:
    """Stream log's first line lacks a top-level model — fall back cleanly.
    Covers the case where the log format drifts."""
    sid = "no-model"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    log_path = tmp_dir / "cc-session-nomodel.log"
    _write_stream_log(tmp_dir, sid, log_path, {"type": "system", "subtype": "init"})
    (project_dir / f"{sid}.jsonl").write_text(
        json.dumps({"message": {"role": "assistant", "model": "claude-sonnet-4-6", "content": []}})
        + "\n",
        encoding="utf-8",
    )

    assert (
        resolve_cc_session_model(sid, project_dir=project_dir, tmp_dir=tmp_dir)
        == "claude-sonnet-4-6"
    )


def test_resolve_cc_session_model_stream_log_garbage_first_line(tmp_path: Path) -> None:
    """Stream log's first line is invalid JSON — return None from the log
    probe and let the trajectory fallback kick in."""
    sid = "garbage"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    log_path = tmp_dir / "cc-session-garbage.log"
    _write_stream_log(tmp_dir, sid, log_path, "not-json-at-all\n")
    (project_dir / f"{sid}.jsonl").write_text(
        json.dumps({"message": {"role": "assistant", "model": "claude-opus-4-6", "content": []}})
        + "\n",
        encoding="utf-8",
    )

    assert (
        resolve_cc_session_model(sid, project_dir=project_dir, tmp_dir=tmp_dir) == "claude-opus-4-6"
    )


def test_resolve_cc_session_model_without_project_dir(tmp_path: Path) -> None:
    """When project_dir is None, only the stream log is consulted. A
    missing pointer returns None instead of trying to read a trajectory."""
    sid = "no-project-dir"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    assert resolve_cc_session_model(sid, project_dir=None, tmp_dir=tmp_dir) is None


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


# --- discover_pi_sessions ---


def _make_pi_session(
    root: Path,
    relative_path: str,
    timestamp: str,
    session_id: str,
) -> Path:
    """Create a minimal valid native Pi v3 tree session."""
    jsonl = root / relative_path
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": timestamp,
            "cwd": "/workspace/pi-project",
        },
        {
            "type": "session_info",
            "id": f"{session_id}-info",
            "parentId": None,
            "timestamp": timestamp,
            "name": f"session-{session_id}",
        },
        {
            "type": "message",
            "id": f"{session_id}-user",
            "parentId": f"{session_id}-info",
            "timestamp": timestamp,
            "message": {"role": "user", "content": "hello"},
        },
    ]
    jsonl.write_text("".join(json.dumps(record) + "\n" for record in records))
    return jsonl


def test_discover_pi_sessions_recursive_and_chronological(tmp_path: Path) -> None:
    """Pi discovery covers nested default and flat custom-session layouts."""
    earlier = _make_pi_session(
        tmp_path,
        "--workspace-project--/zzz.jsonl",
        "2026-03-05T08:00:00Z",
        "pi-earlier",
    )
    later = _make_pi_session(
        tmp_path,
        "run-sh/aaa.jsonl",
        "2026-03-05T20:00:00Z",
        "pi-later",
    )
    _make_pi_session(
        tmp_path,
        "run-sh/outside.jsonl",
        "2026-03-04T20:00:00Z",
        "pi-outside",
    )

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [earlier.resolve(), later.resolve()]


def test_discover_pi_sessions_direct_env_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    direct = tmp_path / "direct"
    agent = tmp_path / "agent"
    direct_session = _make_pi_session(direct, "direct.jsonl", "2026-03-05T10:00:00Z", "pi-direct")
    _make_pi_session(
        agent / "sessions",
        "agent.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-agent",
    )
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(direct))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5))

    assert result == [direct_session.resolve()]


def test_discover_pi_sessions_keeps_named_user_env_path_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi only expands bare ``~``/``~/``; ``~username`` stays cwd-relative."""
    literal_sessions = tmp_path / "~pi-user-that-does-not-exist" / "sessions"
    session = _make_pi_session(
        literal_sessions,
        "literal.jsonl",
        "2026-03-05T10:00:00Z",
        "pi-literal-env",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", "~pi-user-that-does-not-exist/sessions")

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5))

    assert result == [session.resolve()]


def test_discover_pi_sessions_agent_dir_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    session = _make_pi_session(
        agent / "sessions",
        "nested/session.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-agent",
    )
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5))

    assert result == [session.resolve()]


def test_discover_pi_sessions_uses_project_settings_session_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pi's project setting overrides its global setting and agent default."""
    agent = tmp_path / "agent"
    global_sessions = tmp_path / "global-sessions"
    project_sessions = tmp_path / ".pi" / "custom-sessions"
    agent.mkdir()
    (tmp_path / ".pi").mkdir()
    (agent / "settings.json").write_text(json.dumps({"sessionDir": str(global_sessions)}))
    (tmp_path / ".pi" / "settings.json").write_text(
        json.dumps({"sessionDir": ".pi/custom-sessions"})
    )
    _make_pi_session(
        global_sessions,
        "global.jsonl",
        "2026-03-05T10:00:00Z",
        "pi-global",
    )
    project_session = _make_pi_session(
        project_sessions,
        "project.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-project",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5))

    assert result == [project_session.resolve()]


def test_discover_pi_sessions_keeps_named_user_setting_path_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    (tmp_path / ".pi").mkdir()
    (tmp_path / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "~bob/pi-literal"}))
    session = _make_pi_session(
        tmp_path / "~bob" / "pi-literal",
        "literal.jsonl",
        "2026-03-05T10:00:00Z",
        "pi-literal-setting",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5))

    assert result == [session.resolve()]


def test_discover_pi_sessions_uses_global_settings_session_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    configured_sessions = tmp_path / "configured-sessions"
    agent.mkdir()
    (agent / "settings.json").write_text(json.dumps({"sessionDir": str(configured_sessions)}))
    session = _make_pi_session(
        configured_sessions,
        "configured.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-configured",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5))

    assert result == [session.resolve()]


def test_discover_pi_sessions_isolates_recursive_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = tmp_path / "agent"
    sessions = agent / "sessions"
    agent.mkdir()
    (agent / "settings.json").write_text("[" * 10000 + "0" + "]" * 10000)
    session = _make_pi_session(
        sessions,
        "default.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-default",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5))

    assert result == [session.resolve()]
    assert "Ignoring unreadable Pi global settings" in caplog.text


def test_discover_pi_sessions_keeps_tiny_noop_and_does_not_mutate(tmp_path: Path) -> None:
    """Small genuine sessions are retained; discovery only reads source bytes."""
    session = _make_pi_session(
        tmp_path,
        "tiny.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-tiny",
    )
    session.chmod(0o600)
    before_bytes = session.read_bytes()
    before_stat = session.stat()
    assert before_stat.st_size < 4096

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    after_stat = session.stat()
    assert result == [session.resolve()]
    assert session.read_bytes() == before_bytes
    assert after_stat.st_ino == before_stat.st_ino
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "partial_tail",
    [
        b'{"type":"message","id":"still-writing"',
        b'{"type":"message","id":"still-writing","content":"\xf0\x9f',
    ],
)
def test_discover_pi_sessions_keeps_stable_prefix_during_active_append(
    tmp_path: Path, partial_tail: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """An unterminated JSON/UTF-8 tail cannot temporarily hide a live session."""
    session = _make_pi_session(
        tmp_path,
        "active.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-active",
    )
    with session.open("ab") as session_file:
        session_file.write(partial_tail)

    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [session.resolve()]
    assert extract_session_name("pi", session) == "session-pi-active"
    assert "Reading stable prefix" in caplog.text


def test_discover_pi_sessions_finds_real_native_noops() -> None:
    fixture_root = Path(__file__).parent / "fixtures" / "pi"
    noop_codex = (fixture_root / "noop-codex.jsonl").resolve()
    noop_xai = (fixture_root / "noop-xai.jsonl").resolve()
    assert noop_codex.stat().st_size < 4096
    assert noop_xai.stat().st_size < 4096

    result = discover_pi_sessions(date(2026, 8, 31), date(2026, 8, 31), pi_dir=fixture_root)

    assert noop_codex in result
    assert noop_xai in result


def test_discover_pi_sessions_ignores_unrelated_jsonl(tmp_path: Path) -> None:
    unrelated = tmp_path / "events.jsonl"
    unrelated.write_text(json.dumps({"type": "session.start", "timestamp": "2026-03-05"}) + "\n")

    assert discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path) == []


@pytest.mark.parametrize(
    "records,error_match",
    [
        (
            [
                {
                    "type": "session",
                    "version": 4,
                    "id": "future",
                    "timestamp": "2026-03-05T10:00:00Z",
                    "cwd": "/workspace",
                }
            ],
            "unsupported Pi session version",
        ),
        (
            [
                {
                    "type": "session",
                    "version": 3,
                    "id": "print-stream",
                    "timestamp": "2026-03-05T10:00:00Z",
                    "cwd": "/workspace",
                },
                {"type": "agent_start", "timestamp": "2026-03-05T10:00:01Z"},
            ],
            "unsupported Pi v3 entry type",
        ),
    ],
)
def test_discover_pi_sessions_rejects_non_native_pi_formats(
    tmp_path: Path, records: list[dict], error_match: str, caplog: pytest.LogCaptureFixture
) -> None:
    candidate = tmp_path / "candidate.jsonl"
    candidate.write_text("".join(json.dumps(record) + "\n" for record in records))
    valid = _make_pi_session(
        tmp_path,
        "valid.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-valid",
    )

    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [valid.resolve()]
    assert error_match in caplog.text


def test_discover_pi_sessions_isolates_unhashable_entry_type(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    malformed = tmp_path / "unhashable-entry-type.jsonl"
    malformed.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "unhashable-entry-type",
                "timestamp": "2026-03-05T10:00:00Z",
                "cwd": "/workspace",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": [],
                "id": "bad-entry",
                "parentId": None,
                "timestamp": "2026-03-05T10:00:01Z",
            }
        )
        + "\n"
    )
    valid = _make_pi_session(
        tmp_path,
        "valid.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-valid",
    )

    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [valid.resolve()]
    assert "unsupported Pi v3 entry type []" in caplog.text


def test_discover_pi_sessions_rejects_malformed_native_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    candidate = tmp_path / "malformed.jsonl"
    candidate.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "malformed",
                "timestamp": "2026-03-05T10:00:00Z",
                "cwd": "/workspace",
            }
        )
        + "\n{not-json\n"
    )
    valid = _make_pi_session(
        tmp_path,
        "valid.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-valid",
    )

    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [valid.resolve()]
    assert "invalid JSON on line 2" in caplog.text


@pytest.mark.parametrize(
    "invalid_value",
    [
        "NaN",
        "1e400",
        "[" * 10000 + "0" + "]" * 10000,
    ],
    ids=["nonfinite-constant", "nonfinite-exponent", "recursive"],
)
def test_discover_pi_sessions_isolates_strict_json_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    invalid_value: str,
) -> None:
    malformed = tmp_path / "strict-json-failure.jsonl"
    malformed.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "strict-json-failure",
                "timestamp": "2026-03-05T10:00:00Z",
                "cwd": "/workspace",
            }
        )
        + "\n"
        + '{"type":"custom","id":"bad","parentId":null,'
        + '"timestamp":"2026-03-05T10:00:01Z","value":'
        + invalid_value
        + "}\n"
    )
    valid = _make_pi_session(
        tmp_path,
        "valid.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-valid",
    )

    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [valid.resolve()]
    assert "invalid JSON on line 2" in caplog.text


def test_discover_pi_sessions_skips_invalid_timestamp_but_keeps_sibling(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    invalid = tmp_path / "invalid-timestamp.jsonl"
    invalid.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "invalid-timestamp",
                "timestamp": "not-a-timestamp",
                "cwd": "/workspace",
            }
        )
        + "\n"
    )
    valid = _make_pi_session(
        tmp_path,
        "valid.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-valid",
    )

    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [valid.resolve()]
    assert "invalid header timestamp" in caplog.text


def test_discover_pi_sessions_skips_overflowing_utc_timestamp_but_keeps_sibling(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    overflowing = tmp_path / "overflowing-timestamp.jsonl"
    overflowing.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": "overflowing-timestamp",
                "timestamp": "9999-12-31T23:59:59-14:00",
                "cwd": "/workspace",
            }
        )
        + "\n"
    )
    valid = _make_pi_session(
        tmp_path,
        "valid.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-valid",
    )

    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [valid.resolve()]
    assert "invalid header timestamp" in caplog.text


def test_discover_pi_sessions_filters_old_headers_before_full_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gptme_sessions.discovery as discovery_module

    old = _make_pi_session(
        tmp_path,
        "old.jsonl",
        "2020-03-05T11:00:00Z",
        "pi-old",
    )
    current = _make_pi_session(
        tmp_path,
        "current.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-current",
    )
    real_load = discovery_module._load_pi_native_records
    fully_loaded: list[Path] = []

    def recording_load(path: Path) -> list[dict] | None:
        fully_loaded.append(path)
        return real_load(path)

    monkeypatch.setattr(discovery_module, "_load_pi_native_records", recording_load)

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [current.resolve()]
    assert fully_loaded == [current]
    assert old not in fully_loaded


def test_discover_pi_sessions_deduplicates_canonical_aliases(tmp_path: Path) -> None:
    target = _make_pi_session(
        tmp_path,
        "target.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-target",
    )
    (tmp_path / "alias-a.jsonl").symlink_to(target)
    (tmp_path / "alias-b.jsonl").symlink_to(target)

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [target.resolve()]


def test_discover_pi_sessions_skips_unreadable_file_but_keeps_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unreadable = _make_pi_session(
        tmp_path,
        "unreadable.jsonl",
        "2026-03-05T10:00:00Z",
        "pi-unreadable",
    )
    valid = _make_pi_session(
        tmp_path,
        "valid.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-valid",
    )
    real_open = Path.open

    def selective_open(path: Path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("test permission denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", selective_open)
    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [valid.resolve()]
    assert "Skipping unreadable Pi session candidate" in caplog.text


def test_discover_pi_sessions_walk_error_keeps_readable_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import gptme_sessions.discovery as discovery_module

    valid = _make_pi_session(
        tmp_path,
        "valid.jsonl",
        "2026-03-05T11:00:00Z",
        "pi-valid",
    )
    real_walk = discovery_module.os.walk

    def walk_with_error(*args, onerror=None, **kwargs):
        assert onerror is not None
        onerror(PermissionError("blocked test subtree"))
        yield from real_walk(*args, onerror=onerror, **kwargs)

    monkeypatch.setattr(discovery_module.os, "walk", walk_with_error)
    with caplog.at_level(logging.WARNING):
        result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5), pi_dir=tmp_path)

    assert result == [valid.resolve()]
    assert "Skipping unreadable Pi session directory" in caplog.text


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

    def test_pi_uses_active_session_name(self, tmp_path: Path) -> None:
        """pi: uses the latest active session_info name."""
        session = _make_pi_session(
            tmp_path,
            "timestamped-name.jsonl",
            "2026-03-05T11:00:00Z",
            "pi-native-session-id",
        )
        assert extract_session_name("pi", session) == "session-pi-native-session-id"

    def test_pi_falls_back_to_full_header_id(self, tmp_path: Path) -> None:
        """pi: header-only sessions keep the complete native ID as their name."""
        session = tmp_path / "header-only.jsonl"
        session.write_text(
            json.dumps(
                {
                    "type": "session",
                    "version": 3,
                    "id": "pi-complete-native-id",
                    "timestamp": "2026-03-05T11:00:00Z",
                    "cwd": "/workspace",
                }
            )
            + "\n"
        )
        assert extract_session_name("pi", session) == "pi-complete-native-id"

    def test_pi_name_follows_active_branch(self, tmp_path: Path) -> None:
        """pi: a name on an abandoned branch must not replace the active name."""
        timestamp = "2026-03-05T11:00:00Z"
        session = tmp_path / "branched.jsonl"
        records = [
            {
                "type": "session",
                "version": 3,
                "id": "pi-branched",
                "timestamp": timestamp,
                "cwd": "/workspace",
            },
            {
                "type": "session_info",
                "id": "root",
                "parentId": None,
                "timestamp": timestamp,
                "name": "root-name",
            },
            {
                "type": "message",
                "id": "common",
                "parentId": "root",
                "timestamp": timestamp,
                "message": {"role": "user", "content": "branch"},
            },
            {
                "type": "session_info",
                "id": "abandoned-name",
                "parentId": "common",
                "timestamp": timestamp,
                "name": "abandoned",
            },
            {
                "type": "message",
                "id": "abandoned-leaf",
                "parentId": "abandoned-name",
                "timestamp": timestamp,
                "message": {"role": "assistant", "content": "old"},
            },
            {
                "type": "session_info",
                "id": "active-name",
                "parentId": "common",
                "timestamp": timestamp,
                "name": "active",
            },
            {
                "type": "message",
                "id": "active-leaf",
                "parentId": "active-name",
                "timestamp": timestamp,
                "message": {"role": "assistant", "content": "current"},
            },
        ]
        session.write_text("".join(json.dumps(record) + "\n" for record in records))

        assert extract_session_name("pi", session) == "active"


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

    def test_codex_empty_file(self, tmp_path: Path) -> None:
        """codex: returns None when JSONL is empty."""
        jsonl = tmp_path / "session.jsonl"
        jsonl.touch()
        assert extract_project("codex", jsonl) is None

    def test_codex_extracts_cwd(self, tmp_path: Path) -> None:
        """codex: extracts cwd from the first event's payload."""
        jsonl = tmp_path / "session.jsonl"
        event = {
            "type": "session_meta",
            "payload": {"cwd": "/home/bob/bob"},
        }
        jsonl.write_text(json.dumps(event) + "\n")
        assert extract_project("codex", jsonl) == "/home/bob/bob"

    def test_codex_large_first_line_uses_bounded_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """codex: uses a bounded read instead of readline() for huge first lines."""
        jsonl = tmp_path / "session.jsonl"
        oversized_event = json.dumps(
            {"type": "session_meta", "payload": {"cwd": "/home/bob/bob", "blob": "x" * 9000}}
        )

        class FakeFile:
            def __enter__(self) -> FakeFile:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

            def read(self, size: int = -1) -> str:
                assert size == 8192
                return oversized_event[:size]

            def readline(self) -> str:
                raise AssertionError("_first_event_cwd should not call readline()")

        def fake_open(path: Path, encoding: str = "utf-8") -> FakeFile:
            assert path == jsonl
            assert encoding == "utf-8"
            return FakeFile()

        monkeypatch.setattr(Path, "open", fake_open)

        assert _first_event_cwd(jsonl) is None

    def test_codex_non_dict_first_event_returns_none(self, tmp_path: Path) -> None:
        """codex: returns None when the first JSONL value is not an object."""
        jsonl = tmp_path / "session.jsonl"
        jsonl.write_text('["not", "an", "object"]\n')
        assert extract_project("codex", jsonl) is None

    def test_codex_non_string_cwd_returns_none(self, tmp_path: Path) -> None:
        """codex: returns None when payload.cwd is not a string."""
        jsonl = tmp_path / "session.jsonl"
        event = {
            "type": "session_meta",
            "payload": {"cwd": 123},
        }
        jsonl.write_text(json.dumps(event) + "\n")
        assert extract_project("codex", jsonl) is None

    def test_codex_invalid_json_returns_none(self, tmp_path: Path) -> None:
        """codex: returns None when the first line is not valid JSON."""
        jsonl = tmp_path / "session.jsonl"
        jsonl.write_text("not valid json{{{[[[")
        assert extract_project("codex", jsonl) is None

    def test_copilot_returns_none(self, tmp_path: Path) -> None:
        """copilot: returns None (no project info available)."""
        events = tmp_path / "events.jsonl"
        events.touch()
        assert extract_project("copilot", events) is None

    def test_pi_extracts_header_cwd(self, tmp_path: Path) -> None:
        """pi: extracts the working directory from the native session header."""
        session = _make_pi_session(
            tmp_path,
            "session.jsonl",
            "2026-03-05T11:00:00Z",
            "pi-project",
        )
        assert extract_project("pi", session) == "/workspace/pi-project"


# --- _expand_pi_path (file:// URI handling) ---


def test_expand_pi_path_plain_path(tmp_path: Path) -> None:
    """Plain paths pass through unchanged."""
    assert _expand_pi_path(str(tmp_path)) == tmp_path


def test_expand_pi_path_tilde_home() -> None:
    """Tilde-only resolves to home directory."""
    assert _expand_pi_path("~") == Path.home()


def test_expand_pi_path_tilde_subdir() -> None:
    """~/subdir resolves relative to home."""
    assert _expand_pi_path("~/pi-sessions") == Path.home() / "pi-sessions"


def test_expand_pi_path_file_uri_absolute(tmp_path: Path) -> None:
    """file:///absolute/path resolves to the filesystem path."""
    uri = f"file://{tmp_path}"
    assert _expand_pi_path(uri) == tmp_path


def test_expand_pi_path_file_uri_percent_encoded() -> None:
    """Percent-escaped file URI paths are decoded before use."""
    assert _expand_pi_path("file:///tmp/my%20sessions") == Path("/tmp/my sessions")


def test_expand_pi_path_windows_drive_letter_is_not_a_scheme() -> None:
    """Windows absolute paths must not be rejected as URI schemes.

    urlparse("C:\\\\Users\\\\me") reports scheme "c"; gating URI parsing on
    "://" / "file:" keeps these as ordinary filesystem paths.
    """
    assert _expand_pi_path(r"C:\Users\me\sessions") == Path(r"C:\Users\me\sessions")
    assert _expand_pi_path("C:/Users/me/sessions") == Path("C:/Users/me/sessions")


def test_expand_pi_path_file_uri_empty_path_fails_closed() -> None:
    """Malformed file:// URI with no path component raises ValueError."""
    with pytest.raises(ValueError, match="Malformed"):
        _expand_pi_path("file://")


def test_expand_pi_path_non_file_scheme_fails_closed() -> None:
    """Non-file URI schemes (http, https, etc.) raise ValueError."""
    with pytest.raises(ValueError, match="Unsupported"):
        _expand_pi_path("http://example.com/sessions")


def test_discover_pi_sessions_file_uri_session_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PI_CODING_AGENT_SESSION_DIR=file:///... resolves and discovers sessions."""
    session = _make_pi_session(tmp_path, "session.jsonl", "2026-03-05T10:00:00Z", "pi-uri")
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", f"file://{tmp_path}")
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)

    result = discover_pi_sessions(date(2026, 3, 5), date(2026, 3, 5))

    assert result == [session.resolve()]


# --- archive roots (rotated-out sessions) ---


def _cc_session(root: Path, project: str, sid: str, day: str = "2026-03-05") -> Path:
    project_dir = root / project
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{sid}.jsonl"
    path.write_text(
        json.dumps({"sessionId": sid, "timestamp": f"{day}T10:00:00Z", "type": "user"})
        + "\n"
        + json.dumps(
            {
                "sessionId": sid,
                "timestamp": f"{day}T10:00:01Z",
                "type": "assistant",
                "message": {"role": "assistant", "model": "claude-archived-1"},
            }
        )
        + "\n"
    )
    return path


def test_discover_cc_sessions_extra_dirs_param_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "projects"
    archive = tmp_path / "archive"
    live_file = _cc_session(live, "-home-u-repo", "aaaa-live")
    archived_file = _cc_session(archive, "-home-u-repo", "bbbb-archived")
    # same session id in both roots: the live copy wins, reported once
    _cc_session(archive, "-home-u-repo", "aaaa-live", day="2026-03-05")

    result = discover_cc_sessions(
        date(2026, 3, 5), date(2026, 3, 5), cc_dir=live, min_size=0, extra_dirs=[archive]
    )
    assert result == sorted([live_file, archived_file])

    # env-driven default
    monkeypatch.setenv("GPTME_CC_EXTRA_PROJECTS_DIRS", str(archive))
    result_env = discover_cc_sessions(date(2026, 3, 5), date(2026, 3, 5), cc_dir=live, min_size=0)
    assert result_env == result

    # explicit empty list disables the env default
    assert discover_cc_sessions(
        date(2026, 3, 5), date(2026, 3, 5), cc_dir=live, min_size=0, extra_dirs=[]
    ) == [live_file]


def test_find_cc_session_file_prefers_live_then_archive(tmp_path: Path) -> None:
    live = tmp_path / "projects"
    archive = tmp_path / "archive"
    (live / "-home-u-repo").mkdir(parents=True)
    archived = _cc_session(archive, "-home-u-repo", "cccc")
    assert find_cc_session_file("cccc", cc_dir=live, extra_dirs=[archive]) == archived
    assert (
        find_cc_session_file("cccc", cc_dir=live, extra_dirs=[archive], project="-home-u-repo")
        == archived
    )
    assert find_cc_session_file("cccc", cc_dir=live, extra_dirs=[archive], project="-other") is None
    revived = _cc_session(live, "-home-u-repo", "cccc")
    assert find_cc_session_file("cccc", cc_dir=live, extra_dirs=[archive]) == revived
    assert find_cc_session_file("", cc_dir=live, extra_dirs=[archive]) is None
    assert find_cc_session_file("zzzz", cc_dir=live, extra_dirs=[archive]) is None


def test_resolve_cc_session_model_falls_back_to_archive_root(tmp_path: Path) -> None:
    live = tmp_path / "projects" / "-home-u-repo"
    live.mkdir(parents=True)
    _cc_session(tmp_path / "archive", "-home-u-repo", "dddd")
    empty_tmp = tmp_path / "tmp"
    empty_tmp.mkdir()
    assert (
        resolve_cc_session_model(
            "dddd", project_dir=live, tmp_dir=empty_tmp, extra_dirs=[tmp_path / "archive"]
        )
        == "claude-archived-1"
    )
    assert (
        resolve_cc_session_model("dddd", project_dir=live, tmp_dir=empty_tmp, extra_dirs=[]) is None
    )
