"""Tests for cc_session_data module."""

import json
from datetime import date
from pathlib import Path

from gptme_activity_summary.cc_session_data import (
    _decode_project_path,
    _parse_cc_session,
    _session_date_from_first_line,
    fetch_cc_session_stats_range,
)


def test_decode_project_path():
    """Test decoding CC project directory names to real paths."""
    assert _decode_project_path("-home-bob-bob") == "/home/bob/bob"
    assert _decode_project_path("-home-bob-bob-gptme-contrib") == "/home/bob/bob/gptme/contrib"
    assert _decode_project_path("-Users-erb-Programming-gptme") == "/Users/erb/Programming/gptme"
    # Non-encoded paths pass through
    assert _decode_project_path("plain") == "plain"


def _make_session_jsonl(
    tmp_path: Path, project: str, session_id: str, messages: list[dict]
) -> Path:
    """Helper to create a CC session JSONL file with test data."""
    project_dir = tmp_path / project
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = project_dir / f"{session_id}.jsonl"
    with open(jsonl_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")
    return jsonl_path


def _make_user_msg(timestamp: str, content: str = "test", bypass: bool = False) -> dict:
    """Create a CC user message entry."""
    entry: dict = {
        "type": "user",
        "timestamp": timestamp,
        "cwd": "/home/bob/bob",
        "message": {"role": "user", "content": content},
    }
    if bypass:
        entry["permissionMode"] = "bypassPermissions"
    return entry


def _make_assistant_msg(
    timestamp: str,
    model: str = "claude-opus-4-6",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> dict:
    """Create a CC assistant message entry."""
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": "response"}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def test_session_date_from_first_line(tmp_path):
    """Test quick date extraction from first line of JSONL."""
    jsonl_path = _make_session_jsonl(
        tmp_path,
        "proj",
        "abc123",
        [_make_user_msg("2026-02-17T05:00:00Z")],
    )
    result = _session_date_from_first_line(jsonl_path)
    assert result == date(2026, 2, 17)


def test_session_date_from_first_line_missing(tmp_path):
    """Test date extraction when no timestamp present."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    jsonl_path = project_dir / "abc.jsonl"
    jsonl_path.write_text('{"type": "summary"}\n')
    result = _session_date_from_first_line(jsonl_path)
    assert result is None


def test_parse_cc_session(tmp_path):
    """Test parsing a synthetic CC session JSONL."""
    messages = [
        _make_user_msg("2026-02-17T10:00:00Z", bypass=True),
        _make_assistant_msg("2026-02-17T10:01:00Z", input_tokens=200, output_tokens=100),
        _make_user_msg("2026-02-17T10:02:00Z"),
        _make_assistant_msg("2026-02-17T10:03:00Z", input_tokens=300, output_tokens=150),
    ]
    jsonl_path = _make_session_jsonl(tmp_path, "proj", "session1", messages)

    info = _parse_cc_session(jsonl_path)
    assert info.name == "session1"
    assert info.model == "claude-opus-4-6"
    assert info.input_tokens == 500  # 200 + 300
    assert info.output_tokens == 250  # 100 + 150
    assert info.message_count == 4  # 2 user + 2 assistant
    assert info.cost == 0.0  # subscription
    assert info.interactive is False  # bypassPermissions
    assert info.workspace == "/home/bob/bob"
    assert info.duration_seconds == 180.0  # 3 minutes


def test_parse_cc_session_interactive(tmp_path):
    """Test that sessions without bypassPermissions are interactive."""
    messages = [
        _make_user_msg("2026-02-17T10:00:00Z", bypass=False),
        _make_assistant_msg("2026-02-17T10:01:00Z"),
    ]
    jsonl_path = _make_session_jsonl(tmp_path, "proj", "session2", messages)

    info = _parse_cc_session(jsonl_path)
    assert info.interactive is True


def test_fetch_cc_session_stats_range(tmp_path):
    """Test full pipeline: scan projects, filter by date, aggregate."""
    # Session on Feb 17
    messages_17 = [
        _make_user_msg("2026-02-17T10:00:00Z"),
        _make_assistant_msg(
            "2026-02-17T10:05:00Z", model="claude-opus-4-6", input_tokens=1000, output_tokens=500
        ),
    ]
    # Session on Feb 16 (should be excluded when querying Feb 17 only)
    messages_16 = [
        _make_user_msg("2026-02-16T10:00:00Z"),
        _make_assistant_msg(
            "2026-02-16T10:05:00Z",
            model="claude-sonnet-4-5-20250929",
            input_tokens=200,
            output_tokens=100,
        ),
    ]
    _make_session_jsonl(tmp_path, "-home-bob-bob", "sess-a", messages_17)
    _make_session_jsonl(tmp_path, "-home-bob-bob", "sess-b", messages_16)

    # Query just Feb 17
    stats = fetch_cc_session_stats_range(date(2026, 2, 17), date(2026, 2, 17), cc_dir=tmp_path)
    assert stats.session_count == 1
    assert stats.total_input_tokens == 1000
    assert stats.total_output_tokens == 500
    assert "claude-opus-4-6" in stats.models_used

    # Query full range
    stats_all = fetch_cc_session_stats_range(date(2026, 2, 16), date(2026, 2, 17), cc_dir=tmp_path)
    assert stats_all.session_count == 2
    assert stats_all.total_input_tokens == 1200
    assert "claude-opus-4-6" in stats_all.models_used
    assert "claude-sonnet-4-5-20250929" in stats_all.models_used


def test_fetch_cc_empty_dir(tmp_path):
    """Test graceful handling of empty/missing directory."""
    stats = fetch_cc_session_stats_range(
        date(2026, 2, 17), date(2026, 2, 17), cc_dir=tmp_path / "nonexistent"
    )
    assert stats.session_count == 0
    assert stats.total_tokens == 0


def test_fetch_cc_empty_project_dir(tmp_path):
    """Test graceful handling of project dir with no JSONL files."""
    (tmp_path / "some-project").mkdir()
    stats = fetch_cc_session_stats_range(date(2026, 2, 17), date(2026, 2, 17), cc_dir=tmp_path)
    assert stats.session_count == 0


def test_multiple_projects(tmp_path):
    """Test aggregation across multiple project directories."""
    messages_a = [
        _make_user_msg("2026-02-17T10:00:00Z"),
        _make_assistant_msg("2026-02-17T10:05:00Z", input_tokens=100, output_tokens=50),
    ]
    messages_b = [
        _make_user_msg("2026-02-17T11:00:00Z"),
        _make_assistant_msg("2026-02-17T11:05:00Z", input_tokens=200, output_tokens=100),
    ]
    _make_session_jsonl(tmp_path, "-home-bob-bob", "sess-1", messages_a)
    _make_session_jsonl(tmp_path, "-home-bob-other", "sess-2", messages_b)

    stats = fetch_cc_session_stats_range(date(2026, 2, 17), date(2026, 2, 17), cc_dir=tmp_path)
    assert stats.session_count == 2
    assert stats.total_input_tokens == 300
    assert stats.total_output_tokens == 150
