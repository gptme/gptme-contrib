"""Tests for session_data module."""

import json
from datetime import date
from pathlib import Path

from gptme_activity_summary.session_data import (
    ModelBreakdown,
    SessionInfo,
    SessionStats,
    _session_matches_date,
    _session_matches_range,
    fetch_session_stats,
    fetch_session_stats_range,
    format_sessions_for_prompt,
    merge_session_stats,
)


def test_session_matches_date():
    """Test session name matching for a specific date."""
    assert _session_matches_date("2025-01-15-session1", date(2025, 1, 15))
    assert not _session_matches_date("2025-01-16-session1", date(2025, 1, 15))
    assert not _session_matches_date("invalid-name", date(2025, 1, 15))


def test_session_matches_range():
    """Test session name matching for a date range."""
    assert _session_matches_range("2025-01-15-session1", date(2025, 1, 10), date(2025, 1, 20))
    assert _session_matches_range("2025-01-10-session1", date(2025, 1, 10), date(2025, 1, 20))
    assert _session_matches_range("2025-01-20-session1", date(2025, 1, 10), date(2025, 1, 20))
    assert not _session_matches_range("2025-01-09-session1", date(2025, 1, 10), date(2025, 1, 20))
    assert not _session_matches_range("2025-01-21-session1", date(2025, 1, 10), date(2025, 1, 20))
    assert not _session_matches_range("invalid", date(2025, 1, 10), date(2025, 1, 20))


def test_format_sessions_empty():
    """Test formatting with no sessions."""
    stats = SessionStats(start_date=date(2025, 1, 1), end_date=date(2025, 1, 1))
    result = format_sessions_for_prompt(stats)
    assert result == ""


def test_format_sessions_with_data():
    """Test formatting with session data."""
    stats = SessionStats(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 1),
        session_count=3,
        models_used={"claude-3-opus": 2, "claude-3-sonnet": 1},
        total_input_tokens=10000,
        total_output_tokens=5000,
        total_cost=1.50,
        total_duration_seconds=7200,
    )
    result = format_sessions_for_prompt(stats)
    assert "Session Data (Real Data)" in result
    assert "Sessions**: 3" in result
    assert "claude-3-opus (2x)" in result
    assert "Total tokens**: 15,000" in result
    assert "$1.50" in result
    assert "2.0h" in result


def test_fetch_session_stats_nonexistent_dir():
    """Test fetching stats from a nonexistent directory."""
    stats = fetch_session_stats(date(2025, 1, 15), logs_dir=Path("/nonexistent/path"))
    assert stats.session_count == 0
    assert stats.total_tokens == 0


def test_fetch_session_stats_with_logs(tmp_path):
    """Test fetching stats from a directory with session logs."""
    # Create a session directory
    session_dir = tmp_path / "2025-01-15-test-session"
    session_dir.mkdir()

    # Create config.toml
    config_content = 'model = "claude-3-opus"\nworkspace = "/home/test"\ninteractive = false\n'
    (session_dir / "config.toml").write_text(config_content)

    # Create conversation.jsonl
    messages = [
        {
            "role": "user",
            "content": "Hello",
            "timestamp": "2025-01-15T10:00:00",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cost": 0.01},
        },
        {
            "role": "assistant",
            "content": "Hi there",
            "timestamp": "2025-01-15T10:05:00",
            "usage": {"input_tokens": 200, "output_tokens": 100, "cost": 0.02},
        },
    ]
    conv_lines = [json.dumps(m) for m in messages]
    (session_dir / "conversation.jsonl").write_text("\n".join(conv_lines))

    stats = fetch_session_stats(date(2025, 1, 15), logs_dir=tmp_path)
    assert stats.session_count == 1
    assert stats.models_used == {"claude-3-opus": 1}
    assert stats.total_input_tokens == 300
    assert stats.total_output_tokens == 150
    assert stats.total_cost == 0.03
    assert stats.total_duration_seconds == 300.0  # 5 minutes


def test_fetch_session_stats_range(tmp_path):
    """Test fetching stats across a date range."""
    # Create sessions on different days
    for day, model in [(15, "opus"), (16, "sonnet")]:
        session_dir = tmp_path / f"2025-01-{day:02d}-session"
        session_dir.mkdir()
        (session_dir / "config.toml").write_text(f'model = "{model}"\n')
        msg = {"role": "user", "content": "test", "timestamp": f"2025-01-{day:02d}T10:00:00"}
        (session_dir / "conversation.jsonl").write_text(json.dumps(msg))

    stats = fetch_session_stats_range(date(2025, 1, 15), date(2025, 1, 16), logs_dir=tmp_path)
    assert stats.session_count == 2
    assert "opus" in stats.models_used
    assert "sonnet" in stats.models_used


def test_session_stats_total_tokens():
    """Test total_tokens computed property."""
    stats = SessionStats(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 1),
        total_input_tokens=1000,
        total_output_tokens=500,
    )
    assert stats.total_tokens == 1500


def test_merge_session_stats():
    """Test merging two SessionStats objects."""
    a = SessionStats(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 1),
        session_count=2,
        models_used={"opus": 2},
        total_input_tokens=1000,
        total_output_tokens=500,
        total_cost=0.50,
        total_duration_seconds=3600,
        sessions=[SessionInfo(name="s1", model="opus")],
    )
    a._model_data["opus"] = ModelBreakdown(
        model="opus", sessions=2, input_tokens=1000, output_tokens=500, cost=0.50
    )

    b = SessionStats(
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 2),
        session_count=1,
        models_used={"opus": 1, "sonnet": 1},
        total_input_tokens=500,
        total_output_tokens=200,
        total_cost=0.0,
        total_duration_seconds=1800,
        sessions=[SessionInfo(name="s2", model="sonnet")],
    )
    b._model_data["opus"] = ModelBreakdown(
        model="opus", sessions=1, input_tokens=300, output_tokens=100, cost=0.0
    )
    b._model_data["sonnet"] = ModelBreakdown(
        model="sonnet", sessions=1, input_tokens=200, output_tokens=100, cost=0.0
    )

    merged = merge_session_stats(a, b)
    assert merged.session_count == 3
    assert merged.start_date == date(2025, 1, 1)
    assert merged.end_date == date(2025, 1, 2)
    assert merged.total_input_tokens == 1500
    assert merged.total_output_tokens == 700
    assert merged.total_cost == 0.50
    assert merged.total_duration_seconds == 5400
    assert len(merged.sessions) == 2
    assert merged.models_used["opus"] == 3
    assert merged.models_used["sonnet"] == 1

    # Check model breakdown
    breakdown = {mb.model: mb for mb in merged.model_breakdown}
    assert breakdown["opus"].sessions == 3
    assert breakdown["opus"].input_tokens == 1300
    assert breakdown["sonnet"].sessions == 1


def test_format_sessions_header():
    """Test that the header no longer says 'gptme'."""
    stats = SessionStats(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 1),
        session_count=1,
        models_used={"opus": 1},
        total_input_tokens=100,
        total_output_tokens=50,
    )
    result = format_sessions_for_prompt(stats)
    assert "## Session Data (Real Data)" in result
    assert "gptme" not in result
