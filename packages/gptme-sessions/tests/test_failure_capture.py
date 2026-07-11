"""Tests for harness failure_reason / error capture."""

from __future__ import annotations

import json
from pathlib import Path


from gptme_sessions.failure_capture import (
    FAILURE_REASON_NONZERO,
    FAILURE_REASON_PRE_RESPONSE,
    FAILURE_REASON_RATE_LIMIT,
    FAILURE_REASON_TIMEOUT,
    _trajectory_has_assistant,
    capture_session_failure,
    classify_failure_reason,
)
from gptme_sessions.post_session import post_session
from gptme_sessions.store import SessionStore


def test_classify_pre_response_fast_fail():
    assert (
        classify_failure_reason(
            exit_code=1,
            duration_seconds=73,
            input_tokens=0,
            has_assistant_turn=False,
            error_text=None,
        )
        == FAILURE_REASON_PRE_RESPONSE
    )


def test_classify_timeout_exit_124():
    assert (
        classify_failure_reason(
            exit_code=124,
            duration_seconds=3600,
            input_tokens=1000,
            has_assistant_turn=True,
            error_text=None,
        )
        == FAILURE_REASON_TIMEOUT
    )


def test_classify_rate_limit_from_stderr():
    assert (
        classify_failure_reason(
            exit_code=1,
            duration_seconds=200,
            input_tokens=500,
            has_assistant_turn=True,
            error_text="HTTP 429 rate limit exceeded",
        )
        == FAILURE_REASON_RATE_LIMIT
    )


def test_capture_from_stderr_tail(tmp_path: Path):
    stderr = tmp_path / "stderr.log"
    stderr.write_text("line1\nOpenAI API error: connection reset\n", encoding="utf-8")
    reason, err = capture_session_failure(
        exit_code=1,
        duration_seconds=30,
        input_tokens=100,
        trajectory_path=None,
        harness_stderr_path=stderr,
    )
    assert reason is not None
    assert err is not None
    assert "connection reset" in err


def test_post_session_records_failure_on_nonzero_exit(tmp_path: Path):
    traj = tmp_path / "conversation.jsonl"
    traj.write_text(
        json.dumps({"role": "user", "content": "hi"}) + "\n",
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=tmp_path / "sessions")
    result = post_session(
        store=store,
        harness="gptme",
        model="gpt-5.5",
        exit_code=1,
        duration_seconds=82,
        trajectory_path=traj,
    )
    assert result.record.outcome == "failed"
    assert result.record.failure_reason == FAILURE_REASON_PRE_RESPONSE
    assert result.record.error is not None


def test_post_session_no_failure_fields_on_success(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path / "sessions")
    result = post_session(
        store=store,
        harness="gptme",
        exit_code=0,
        duration_seconds=10,
    )
    assert result.record.failure_reason is None
    assert result.record.error is None


def test_classify_not_pre_response_when_has_assistant_turn():
    """Zero input_tokens must not override a confirmed assistant turn (Greptile P1)."""
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=60,
        input_tokens=0,
        has_assistant_turn=True,
        error_text=None,
    )
    assert result == FAILURE_REASON_NONZERO


def test_trajectory_has_assistant_cc_nested_format(tmp_path: Path):
    """CC nested assistant records must be detected (Greptile P1)."""
    traj = tmp_path / "conversation.jsonl"
    cc_record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello, how can I help?"}],
        },
    }
    traj.write_text(json.dumps(cc_record) + "\n", encoding="utf-8")
    assert _trajectory_has_assistant(traj) is True


def test_capture_cc_session_with_assistant_not_pre_response(tmp_path: Path):
    """A CC-format trajectory with assistant response must not get pre_response class."""
    traj = tmp_path / "conversation.jsonl"
    cc_record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Sure, I can do that."}],
        },
    }
    traj.write_text(json.dumps(cc_record) + "\n", encoding="utf-8")
    reason, _ = capture_session_failure(
        exit_code=1,
        duration_seconds=45,
        input_tokens=0,
        trajectory_path=traj,
        harness_stderr_path=None,
    )
    assert reason == FAILURE_REASON_NONZERO
