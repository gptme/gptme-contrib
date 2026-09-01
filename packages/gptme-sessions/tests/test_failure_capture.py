"""Tests for harness failure_reason / error capture."""

from __future__ import annotations

import json
from pathlib import Path


from gptme_sessions.failure_capture import (
    FAILURE_REASON_AUTH,
    FAILURE_REASON_INVALID_REQUEST,
    FAILURE_REASON_NONZERO,
    FAILURE_REASON_PRE_RESPONSE,
    FAILURE_REASON_RATE_LIMIT,
    FAILURE_REASON_TIMEOUT,
    _record_has_any_content,
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


def test_trajectory_has_assistant_cc_tool_use_only(tmp_path: Path):
    """CC assistant turns with only tool_use blocks must be detected (Greptile P1)."""
    traj = tmp_path / "conversation.jsonl"
    cc_record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_abc", "name": "Bash", "input": {"cmd": "ls"}}
            ],
        },
    }
    traj.write_text(json.dumps(cc_record) + "\n", encoding="utf-8")
    assert _trajectory_has_assistant(traj) is True


def test_capture_cc_tool_use_only_not_pre_response(tmp_path: Path):
    """A CC assistant turn with only tool_use must not get pre_response_api_failure."""
    traj = tmp_path / "conversation.jsonl"
    cc_record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_abc", "name": "Bash", "input": {}}],
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


def test_classify_auth_not_triggered_by_lesson_name():
    """'Auth Blueprint' in gptme startup lesson list must NOT classify as auth.

    Regression: 'auth' in lower matched lesson names like 'Auth Blueprint'
    injected into gptme stdout, causing valid deepseek 400 errors to be
    misclassified as failure_reason='auth'. (ErikBjare/bob#1116)
    """
    # Realistic gptme stderr that includes lesson list but no real auth error
    error_text = (
        "· Auto-included 20 lessons:\n"
        "- Autonomous Session Workflow\n"
        "- Ship\n"
        "- Auth Blueprint\n"  # ← was triggering the false positive
        "- Lesson Quality Standards\n"
        "· ERROR    provider_error_code: invalid_request_error"
    )
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=90,
        input_tokens=75000,
        has_assistant_turn=False,
        error_text=error_text,
    )
    assert (
        result != FAILURE_REASON_AUTH
    ), "lesson name 'Auth Blueprint' must not trigger auth classification"


def test_classify_invalid_request_deepseek_tool_calls():
    """deepseek 400 invalid_request_error (tool_calls) → FAILURE_REASON_INVALID_REQUEST."""
    error_text = (
        "{'error': {'message': 'tool calls must be followed by tool responses', "
        "'type': 'invalid_request_error', 'provider_error_code': 'invalid_request_error'}}"
    )
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=120,
        input_tokens=70000,
        has_assistant_turn=False,
        error_text=error_text,
    )
    assert result == FAILURE_REASON_INVALID_REQUEST


def test_classify_auth_real_401():
    """Real 401 Unauthorized error text → FAILURE_REASON_AUTH."""
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=30,
        input_tokens=0,
        has_assistant_turn=False,
        error_text="HTTP error: 401 Unauthorized — authentication failed",
    )
    assert result == FAILURE_REASON_AUTH


def test_classify_auth_unauthorized_text():
    """'unauthorized' in error text → FAILURE_REASON_AUTH even without '401'."""
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=30,
        input_tokens=0,
        has_assistant_turn=False,
        error_text="Error: Unauthorized access, check your API key",
    )
    assert result == FAILURE_REASON_AUTH


def test_record_has_any_content_tool_use():
    """_record_has_any_content returns True for tool_use-only CC assistant turns."""
    rec = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_x", "name": "Read", "input": {}}],
        },
    }
    assert _record_has_any_content(rec) is True


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


def test_classify_cc_weekly_limit_copy():
    """CC user-facing weekly-limit copy has 'limit' but not 'rate'."""
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=53,
        input_tokens=0,
        has_assistant_turn=True,
        error_text="You've hit your weekly limit · resets Sep 1, 6pm (UTC)",
    )
    assert result == FAILURE_REASON_RATE_LIMIT


def test_capture_cc_weekly_limit_stream_json(tmp_path: Path):
    """CC seven_day weekly-limit stream-json must classify as rate_limit.

    Live 2026-08-31 email-run storm (13/13 surviving logs): synthetic assistant
    turn + rate_limit_event + api_error_status=429. Content-only extraction
    previously returned nonzero_exit_unclassified because has_assistant_turn
    blocked the pre_response fallback and the visible copy never said 'rate'.
    """
    traj = tmp_path / "conversation.jsonl"
    records = [
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "rateLimitType": "seven_day",
                "overageStatus": "rejected",
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "content": [
                    {
                        "type": "text",
                        "text": "You've hit your weekly limit · resets Sep 1, 6pm (UTC)",
                    }
                ],
            },
            "error": "rate_limit",
            "is_api_error_message": True,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your weekly limit · resets Sep 1, 6pm (UTC)",
            "num_turns": 1,
        },
    ]
    traj.write_text(
        "".join(json.dumps(rec) + "\n" for rec in records),
        encoding="utf-8",
    )
    reason, err = capture_session_failure(
        exit_code=1,
        duration_seconds=53,
        input_tokens=0,
        trajectory_path=traj,
        harness_stderr_path=None,
    )
    assert reason == FAILURE_REASON_RATE_LIMIT
    assert err is not None
    assert "429" in err or "rate_limit" in err or "weekly limit" in err.lower()


def test_capture_allowed_rate_limit_event_not_rate_limit(tmp_path: Path):
    """An informational allowed rate_limit_event must not classify a later exit."""
    traj = tmp_path / "conversation.jsonl"
    records = [
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "rateLimitType": "five_hour",
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Sure, I can do that."}],
            },
        },
    ]
    traj.write_text(
        "".join(json.dumps(rec) + "\n" for rec in records),
        encoding="utf-8",
    )
    reason, _ = capture_session_failure(
        exit_code=1,
        duration_seconds=45,
        input_tokens=0,
        trajectory_path=traj,
        harness_stderr_path=None,
    )
    assert reason == FAILURE_REASON_NONZERO
