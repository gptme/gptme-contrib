from collections import deque

from gptme_voice.realtime.subagent_progress import (
    SILENCE_SECONDS,
    append_progress,
    bound_progress,
    classify_lifecycle,
    completion_status_from_returncode,
    is_meaningful_progress,
    seed_completions_from_timings,
)


def test_log_noise_is_not_meaningful_progress() -> None:
    assert is_meaningful_progress("[12:00:00] INFO    starting gptme") is False
    assert is_meaningful_progress("[INFO] Checking task status") is False
    assert is_meaningful_progress("DEBUG    loading tools") is False
    assert is_meaningful_progress("Warning: Input is not a terminal (fd=0).") is False
    assert is_meaningful_progress("WARNING  Failed to load plugin foo") is False
    assert is_meaningful_progress("") is False


def test_error_logs_and_assistant_text_are_meaningful() -> None:
    assert is_meaningful_progress("[12:00:00] ERROR    Error code: 429") is True
    assert is_meaningful_progress("Assistant: empty connectivity test done") is True
    assert is_meaningful_progress("Best quick answer: no matching journal") is True


def test_lifecycle_startup_until_meaningful_output() -> None:
    assert (
        classify_lifecycle(
            first_meaningful_at=None,
            last_meaningful_at=None,
            now=10.0,
        )
        == "startup"
    )


def test_lifecycle_working_then_silent() -> None:
    assert (
        classify_lifecycle(
            first_meaningful_at=1.0,
            last_meaningful_at=9.0,
            now=10.0,
        )
        == "working"
    )
    assert (
        classify_lifecycle(
            first_meaningful_at=1.0,
            last_meaningful_at=1.5,
            now=1.5 + SILENCE_SECONDS + 0.1,
        )
        == "silent"
    )


def test_bound_progress_keeps_newest_lines() -> None:
    lines = [f"line-{i} " + ("x" * 40) for i in range(12)]
    bounded = bound_progress(lines, max_chars=120)
    assert bounded
    assert bounded[-1].startswith("line-11")
    assert len("\n".join(bounded)) <= 120


def test_append_progress_truncates_and_bounds_deque() -> None:
    truncated: deque[str] = deque(maxlen=8)
    append_progress(truncated, "  " + ("a" * 400) + "  ")
    assert list(truncated) == ["a" * 200]

    buffer: deque[str] = deque(maxlen=2)
    append_progress(buffer, "first")
    append_progress(buffer, "second")
    append_progress(buffer, "third")
    assert list(buffer) == ["second", "third"]


def test_seed_completions_prefix_prior_ids_and_map_timeout() -> None:
    seeded = seed_completions_from_timings(
        [
            {
                "task_id": "task-1",
                "task_preview": "Return nothing.",
                "returncode": 124,
                "timings": {"total_seconds": 61.2},
            },
            {
                "task_id": "task-2",
                "task_preview": "ok",
                "returncode": 0,
                "timings": {"total_seconds": 39.8},
            },
        ]
    )
    assert [row["task_id"] for row in seeded] == ["prior:task-1", "prior:task-2"]
    assert seeded[0]["status"] == "timed_out"
    assert seeded[0]["source"] == "prior_leg"
    assert seeded[0]["output_preview"] is None
    assert seeded[1]["status"] == "success"


def test_completion_status_cancelled_and_killed() -> None:
    assert completion_status_from_returncode(-1, cancelled=True) == "cancelled"
    assert completion_status_from_returncode(137) == "error"
    assert completion_status_from_returncode(143) == "timed_out"
