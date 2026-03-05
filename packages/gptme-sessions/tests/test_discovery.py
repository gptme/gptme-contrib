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
