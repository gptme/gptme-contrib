"""Tests for extract_timings_gptme and timings integration in post_session."""

from pathlib import Path

from gptme_sessions.post_session import post_session
from gptme_sessions.signals import extract_timings_gptme
from gptme_sessions.store import SessionStore


def _make_msg(role: str, timings: dict | None = None) -> dict:
    msg: dict = {"role": role, "content": "test", "timestamp": "2026-01-01T00:00:00+00:00"}
    if timings is not None:
        msg["metadata"] = {"timings": timings}
    return msg


# --- extract_timings_gptme unit tests ---


def test_extract_timings_empty_returns_empty():
    assert extract_timings_gptme([]) == {}


def test_extract_timings_no_timing_metadata_returns_empty():
    msgs = [
        _make_msg("system"),
        _make_msg("user"),
        _make_msg("assistant"),  # no timings in metadata
    ]
    assert extract_timings_gptme(msgs) == {}


def test_extract_timings_basic():
    msgs = [
        _make_msg("user"),
        _make_msg("assistant", timings={"ttft_ms": 500.0, "gen_ms": 2000.0}),
    ]
    result = extract_timings_gptme(msgs)
    assert result["ttft_ms_avg"] == 500.0
    assert result["ttft_ms_p50"] == 500.0
    assert result["gen_ms_total"] == 2000.0
    assert result["timed_turns"] == 1
    assert "tool_ms_total" not in result


def test_extract_timings_with_tool():
    msgs = [
        _make_msg("user"),
        _make_msg("assistant", timings={"ttft_ms": 400.0, "gen_ms": 1500.0, "tool_ms": 800.0}),
    ]
    result = extract_timings_gptme(msgs)
    assert result["tool_ms_total"] == 800.0


def test_extract_timings_multi_turn_averages():
    msgs = [
        _make_msg("user"),
        _make_msg("assistant", timings={"ttft_ms": 200.0, "gen_ms": 1000.0}),
        _make_msg("user"),
        _make_msg("assistant", timings={"ttft_ms": 800.0, "gen_ms": 2000.0}),
    ]
    result = extract_timings_gptme(msgs)
    assert result["ttft_ms_avg"] == 500.0  # (200 + 800) / 2
    assert result["ttft_ms_p50"] == 500.0  # median of [200, 800]
    assert result["gen_ms_total"] == 3000.0
    assert result["timed_turns"] == 2


def test_extract_timings_p50_odd_count():
    msgs = [
        _make_msg("assistant", timings={"ttft_ms": 100.0}),
        _make_msg("assistant", timings={"ttft_ms": 300.0}),
        _make_msg("assistant", timings={"ttft_ms": 500.0}),
    ]
    result = extract_timings_gptme(msgs)
    assert result["ttft_ms_p50"] == 300.0  # middle of [100, 300, 500]


def test_extract_timings_tool_ms_accumulates():
    msgs = [
        _make_msg("assistant", timings={"ttft_ms": 200.0, "tool_ms": 300.0}),
        _make_msg("assistant", timings={"ttft_ms": 400.0, "tool_ms": 700.0}),
    ]
    result = extract_timings_gptme(msgs)
    assert result["tool_ms_total"] == 1000.0


def test_extract_timings_ignores_non_assistant_roles():
    msgs = [
        {"role": "system", "content": "sys", "metadata": {"timings": {"ttft_ms": 999.0}}},
        {"role": "user", "content": "hi", "metadata": {"timings": {"ttft_ms": 999.0}}},
        _make_msg("assistant", timings={"ttft_ms": 100.0, "gen_ms": 500.0}),
    ]
    result = extract_timings_gptme(msgs)
    assert result["ttft_ms_avg"] == 100.0
    assert result["timed_turns"] == 1


def test_extract_timings_partial_fields():
    """Messages with only gen_ms (no TTFT) still contribute gen_ms_total."""
    msgs = [
        _make_msg("assistant", timings={"gen_ms": 1200.0}),
    ]
    result = extract_timings_gptme(msgs)
    assert "ttft_ms_avg" not in result
    assert result["gen_ms_total"] == 1200.0
    assert result["timed_turns"] == 0  # TTFT samples list is empty


def test_extract_timings_ignores_non_dict_timings():
    msgs = [
        {"role": "assistant", "content": "x", "metadata": {"timings": "bad_value"}},
        _make_msg("assistant", timings={"ttft_ms": 200.0}),
    ]
    result = extract_timings_gptme(msgs)
    assert result["timed_turns"] == 1
    assert result["ttft_ms_avg"] == 200.0


# --- Integration test: timings flow through post_session into SessionRecord ---


def test_post_session_timings_populated(tmp_path: Path):
    """Timings from a gptme trajectory land in the SessionRecord."""
    # Write a minimal gptme conversation.jsonl with timings metadata
    import json

    traj = tmp_path / "conversation.jsonl"
    msgs = [
        {"role": "system", "content": "sys", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:01+00:00"},
        {
            "role": "assistant",
            "content": "hello",
            "timestamp": "2026-01-01T00:00:02+00:00",
            "metadata": {
                "model": "claude-sonnet-4-6",
                "timings": {"ttft_ms": 350.0, "gen_ms": 1800.0, "tool_ms": 600.0},
            },
        },
    ]
    traj.write_text("\n".join(json.dumps(m) for m in msgs))

    store = SessionStore(sessions_dir=tmp_path / "sessions")
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        duration_seconds=10,
        trajectory_path=traj,
    )
    rec = result.record
    assert rec.ttft_ms_avg == 350.0
    assert rec.ttft_ms_p50 == 350.0
    assert rec.gen_ms_total == 1800.0
    assert rec.tool_ms_total == 600.0


def test_post_session_timings_absent_for_old_sessions(tmp_path: Path):
    """Sessions without timings metadata leave timing fields as None."""
    import json

    traj = tmp_path / "conversation.jsonl"
    msgs = [
        {"role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:01+00:00"},
        {
            "role": "assistant",
            "content": "hello",
            "timestamp": "2026-01-01T00:00:02+00:00",
            "metadata": {"model": "claude-sonnet-4-6"},
        },
    ]
    traj.write_text("\n".join(json.dumps(m) for m in msgs))

    store = SessionStore(sessions_dir=tmp_path / "sessions")
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        duration_seconds=5,
        trajectory_path=traj,
    )
    rec = result.record
    assert rec.ttft_ms_avg is None
    assert rec.gen_ms_total is None
    assert rec.tool_ms_total is None
