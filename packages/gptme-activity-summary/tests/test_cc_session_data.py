"""Tests for cc_session_data module."""

import json
from datetime import date
from pathlib import Path

from gptme_activity_summary.cc_session_data import (
    _extract_cc_metadata,
    fetch_cc_session_stats_range,
)


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


def test_extract_cc_metadata_basic():
    """Test metadata extraction from CC messages."""
    msgs = [
        _make_user_msg("2026-02-17T10:00:00Z", bypass=True),
        _make_assistant_msg("2026-02-17T10:01:00Z"),
        _make_user_msg("2026-02-17T10:02:00Z"),
        _make_assistant_msg("2026-02-17T10:03:00Z"),
    ]
    meta = _extract_cc_metadata(msgs)
    assert meta["workspace"] == "/home/bob/bob"
    assert meta["interactive"] is False  # bypassPermissions
    assert meta["message_count"] == 4  # 2 user + 2 assistant
    assert meta["duration_seconds"] == 180.0  # 3 minutes


def test_extract_cc_metadata_interactive():
    """Test that sessions without bypassPermissions are interactive."""
    msgs = [
        _make_user_msg("2026-02-17T10:00:00Z", bypass=False),
        _make_assistant_msg("2026-02-17T10:01:00Z"),
    ]
    meta = _extract_cc_metadata(msgs)
    assert meta["interactive"] is True


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
