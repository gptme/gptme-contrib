"""Tests for harness failure_reason / error capture."""

from __future__ import annotations

import json
from pathlib import Path

from gptme_sessions.failure_capture import (
    FAILURE_REASON_AUTH,
    FAILURE_REASON_INVALID_REQUEST,
    FAILURE_REASON_MODEL_STREAM_CRASH,
    FAILURE_REASON_NONZERO,
    FAILURE_REASON_PRE_RESPONSE,
    FAILURE_REASON_QUOTA,
    FAILURE_REASON_RATE_LIMIT,
    FAILURE_REASON_TIMEOUT,
    FAILURE_REASON_UPSTREAM_OVERLOADED,
    _extract_trajectory_error_line,
    _record_has_any_content,
    _structured_error_signals,
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


def test_post_session_records_grok_string_error(tmp_path: Path):
    """A native Grok error must not crash recording and trigger PM's fallback."""
    traj = tmp_path / "grok.jsonl"
    error = "API error (status 402 Payment Required): Grok Build usage balance exhausted"
    traj.write_text(
        json.dumps({"type": "available_commands", "tools": []})
        + "\n"
        + json.dumps({"type": "error", "message": f"Internal error: {error}"})
        + f"\nError: Internal error: {error}\n",
        encoding="utf-8",
    )
    store = SessionStore(sessions_dir=tmp_path / "sessions")
    result = post_session(
        store=store,
        harness="grok-build",
        model="grok-4.6",
        session_id="grok-quota-attempt",
        run_type="monitoring",
        category="pm-react",
        exit_code=1,
        duration_seconds=6,
        trajectory_path=traj,
    )
    records = store.load_all()
    assert len(records) == 1
    assert records[0].session_id == "grok-quota-attempt"
    assert records[0].outcome == "failed"
    assert records[0].failure_reason == FAILURE_REASON_QUOTA
    assert records[0].error is not None
    assert error in records[0].error
    assert result.record.error == records[0].error


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


def test_trajectory_has_assistant_grok_string_message(tmp_path: Path):
    """Grok assistant records may carry their content directly in message."""
    traj = tmp_path / "grok.jsonl"
    traj.write_text(
        json.dumps({"type": "assistant", "message": "Hello from Grok"}) + "\n",
        encoding="utf-8",
    )
    assert _trajectory_has_assistant(traj) is True


def test_trajectory_has_assistant_copilot_message(tmp_path: Path):
    """Copilot stores assistant output under data.content."""
    traj = tmp_path / "events.jsonl"
    traj.write_text(
        json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "turnId": "0",
                    "content": "I completed the task.",
                    "toolRequests": [],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert _trajectory_has_assistant(traj) is True


def test_capture_copilot_quota_event(tmp_path: Path):
    """Copilot session.error quota events must not look like opaque pre-response exits."""
    traj = tmp_path / "events.jsonl"
    records = [
        {"type": "assistant.turn_start", "data": {"turnId": "0"}},
        {"type": "assistant.turn_end", "data": {"turnId": "0"}},
        {
            "type": "session.error",
            "data": {
                "errorType": "quota",
                "message": "You have exceeded your monthly quota",
                "statusCode": 402,
                "errorCode": "quota_exceeded",
            },
        },
    ]
    traj.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    reason, err = capture_session_failure(
        exit_code=1,
        duration_seconds=50,
        input_tokens=None,
        trajectory_path=traj,
        harness_stderr_path=None,
    )

    assert reason == FAILURE_REASON_QUOTA
    assert err is not None
    assert "quota_exceeded" in err


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


def test_classify_quota_grok_spending_limit():
    """Grok 403 spending-limit (run out of credits) → FAILURE_REASON_QUOTA, not auth."""
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=30,
        input_tokens=0,
        has_assistant_turn=False,
        error_text=(
            "Error code: 403 - {'code': 'personal-team-blocked:spending-limit', "
            "'error': 'You have run out of credits or need a Grok subscription. "
            "Add credits at https://console.x.ai'}"
        ),
    )
    assert result == FAILURE_REASON_QUOTA


def test_classify_quota_insufficient_quota():
    """OpenAI-style 429 insufficient_quota → FAILURE_REASON_QUOTA, not rate_limit.

    The status code matters: the generic `429` rate-limit branch runs before the
    auth branch, so a real OpenAI quota-exhaustion error (which carries both
    `429` and `insufficient_quota`) regressed to rate_limit until the quota
    markers were checked first. Keep the status in the fixture so the precedence
    stays covered.
    """
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=30,
        input_tokens=0,
        has_assistant_turn=False,
        error_text=(
            "Error code: 429 - {'error': {'message': 'You exceeded your current "
            "quota, please check your plan and billing details.', "
            "'type': 'insufficient_quota', 'code': 'insufficient_quota'}}"
        ),
    )
    assert result == FAILURE_REASON_QUOTA


def test_classify_quota_http_402_insufficient_credits():
    """HTTP 402 credit exhaustion is deterministic quota failure, not retryable startup loss."""
    messages = (
        "Error code: 402 - Insufficient credits. Add more credits.",
        "API error (status 402 Payment Required): usage balance exhausted",
        "'previous_errors': [{'code': 402, 'message': 'can only afford 16 tokens'}]",
        '"http_status": 402',
        "API error status: 402",
        "copilot session.error type=other; status=402",
    )
    for message in messages:
        result = classify_failure_reason(
            exit_code=1,
            duration_seconds=41,
            input_tokens=0,
            has_assistant_turn=False,
            error_text=message,
        )
        assert result == FAILURE_REASON_QUOTA, message


def test_classify_bare_status_402_is_not_quota():
    """Whitespace-only ``status 402`` is not an HTTP status line.

    Guards the 402 matcher from the extra ``status 402`` alternative that
    classified incidental prose as quota. Delimited forms and the explicit
    ``402 Payment Required`` phrase stay quota (see the sibling test).
    """
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=41,
        input_tokens=0,
        has_assistant_turn=True,
        error_text="status 402 of the account is fine",
    )
    assert result != FAILURE_REASON_QUOTA


def test_classify_invalid_request_http_400_unsupported_model():
    """A precise API 400 is a bad request and must never enter first-response retry."""
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=59,
        input_tokens=0,
        has_assistant_turn=False,
        error_text=(
            "Codex API error 400: The gpt-5.4 model is not supported when using "
            "Codex with a ChatGPT account."
        ),
    )
    assert result == FAILURE_REASON_INVALID_REQUEST


def test_classify_quota_wins_over_http_400_marker():
    """A 400 status line that also carries a quota marker is billing, not bad request."""
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=30,
        input_tokens=0,
        has_assistant_turn=False,
        error_text="HTTP/1.1 400 Bad Request: quota_exceeded for this account",
    )
    assert result == FAILURE_REASON_QUOTA


def test_classify_quota_precedes_rate_limit_marker():
    """429 body that also says 'rate limit' of an account with no credits → quota."""
    result = classify_failure_reason(
        exit_code=1,
        duration_seconds=30,
        input_tokens=0,
        has_assistant_turn=False,
        error_text=(
            "HTTP 429 rate limit exceeded - insufficient_quota: You exceeded your current quota"
        ),
    )
    assert result == FAILURE_REASON_QUOTA


def test_classify_auth_with_incidental_billing_mention():
    """A 401 mentioning billing is auth, not quota.

    Guards the breadth of the quota branch. Because quota is checked before auth,
    any generic billing phrase would mask a credential failure: the bare
    "billing" substring matched "contact billing support", and "billing details"
    matched OpenAI's own 401 body ("Check your plan and billing details."), which
    describes an invalid API key. Neither phrase may appear in the quota markers.
    """
    for message in (
        "Invalid API key. Contact billing support if you believe this is an error.",
        "Incorrect API key provided. Check your plan and billing details.",
    ):
        result = classify_failure_reason(
            exit_code=1,
            duration_seconds=30,
            input_tokens=0,
            has_assistant_turn=False,
            error_text=f"Error code: 401 - {{'error': {{'message': '{message}'}}}}",
        )
        assert result == FAILURE_REASON_AUTH, message


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


def test_capture_cc_camelcase_oauth_error_ignores_prompt_prose(tmp_path: Path):
    """Current CC OAuth errors outrank unrelated failure words in the prompt."""
    traj = tmp_path / "conversation.jsonl"
    records = [
        {
            "type": "attachment",
            "content": "If the selected lane failed, document the blocker.",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Your organization has disabled Claude subscription "
                            "access for Claude Code"
                        ),
                    }
                ],
            },
            "error": "oauth_org_not_allowed",
            "isApiErrorMessage": True,
            "apiErrorStatus": 403,
            "apiErrorCode": "oauth_not_allowed_for_organization",
        },
        {
            "type": "attachment",
            "content": "A second claim denied means the selected lane failed.",
        },
    ]
    traj.write_text(
        "".join(json.dumps(rec) + "\n" for rec in records),
        encoding="utf-8",
    )

    reason, err = capture_session_failure(
        exit_code=1,
        duration_seconds=48,
        input_tokens=0,
        trajectory_path=traj,
        harness_stderr_path=None,
    )

    assert reason == FAILURE_REASON_AUTH
    assert err is not None
    assert "api_error_status:403" in err
    assert "oauth_not_allowed_for_organization" in err
    assert "selected lane failed" not in err


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


def test_classify_upstream_overloaded():
    """Provider model-capacity errors classify distinctly from a generic crash."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=443,
        input_tokens=3_695_970,
        has_assistant_turn=False,
        error_text=(
            "error: codex_error_info:server_overloaded; "
            "Selected model is at capacity. Please try a different model."
        ),
    )
    assert reason == FAILURE_REASON_UPSTREAM_OVERLOADED


def test_classify_model_stream_crash():
    """gptme's OpenAI stream IndexError classifies distinctly from a generic crash."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=63,
        input_tokens=142_000,
        has_assistant_turn=True,
        error_text=(
            "Traceback (most recent call last):\n"
            '  File "gptme/llm/llm_openai.py", line 901, in stream\n'
            "IndexError: list index out of range"
        ),
    )
    assert reason == FAILURE_REASON_MODEL_STREAM_CRASH


def test_list_index_error_without_stream_context_stays_unclassified():
    """An unrelated IndexError must not be mislabeled as a model stream crash."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=63,
        input_tokens=142_000,
        has_assistant_turn=True,
        error_text="IndexError: list index out of range",
    )
    assert reason == FAILURE_REASON_NONZERO


def test_classify_quota_precedes_model_stream_crash():
    """Quota exhaustion still wins when the same blob also has a stream IndexError."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=63,
        input_tokens=142_000,
        has_assistant_turn=True,
        error_text=(
            "Error code: 429 - {'error': {'type': 'insufficient_quota'}}\n"
            "Traceback (most recent call last):\n"
            '  File "gptme/llm/llm_openai.py", line 901, in stream\n'
            "IndexError: list index out of range"
        ),
    )
    assert reason == FAILURE_REASON_QUOTA


def test_classify_traceback_line_429_does_not_swallow_stream_crash():
    """Traceback ``line 429`` is not an HTTP 429; the stream crash still wins."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=63,
        input_tokens=142_000,
        has_assistant_turn=True,
        error_text=(
            "Traceback (most recent call last):\n"
            '  File "gptme/llm/llm_openai.py", line 429, in stream\n'
            "IndexError: list index out of range"
        ),
    )
    assert reason == FAILURE_REASON_MODEL_STREAM_CRASH


def test_classify_http_429_precedes_model_stream_crash():
    """A genuine HTTP 429 is still a rate limit even if the parser also crashed."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=63,
        input_tokens=142_000,
        has_assistant_turn=True,
        error_text=(
            "HTTP/1.1 429 Too Many Requests\n"
            "Traceback (most recent call last):\n"
            '  File "gptme/llm/llm_openai.py", line 901, in stream\n'
            "IndexError: list index out of range"
        ),
    )
    assert reason == FAILURE_REASON_RATE_LIMIT


def test_in_streaming_mode_indexerror_without_llm_openai_stays_unclassified():
    """Bare ``in stream`` must not label an unrelated IndexError as a gptme crash."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=63,
        input_tokens=142_000,
        has_assistant_turn=True,
        error_text="IndexError: list index out of range in streaming mode",
    )
    assert reason == FAILURE_REASON_NONZERO


def test_capture_model_stream_crash_from_harness_stderr(tmp_path: Path):
    """The live f6d3 stderr signature survives the capture path."""
    stderr = tmp_path / "harness.stderr"
    stderr.write_text(
        "ERROR Fatal error occurred\n"
        "ERROR list index out of range\n"
        "ERROR at /opt/gptme/gptme/llm/llm_openai.py:1679 in stream\n",
        encoding="utf-8",
    )

    reason, detail = capture_session_failure(
        exit_code=1,
        duration_seconds=63,
        input_tokens=142_000,
        trajectory_path=None,
        harness_stderr_path=stderr,
    )

    assert reason == FAILURE_REASON_MODEL_STREAM_CRASH
    assert detail is not None
    assert "list index out of range" in detail


def test_classify_rate_limit_precedes_overload():
    """A genuine 429 body that also mentions overload stays rate_limit."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=30,
        input_tokens=100,
        has_assistant_turn=True,
        error_text="429 Too Many Requests: server overloaded",
    )
    assert reason == FAILURE_REASON_RATE_LIMIT


def test_capture_codex_task_complete_overload(tmp_path: Path):
    """Codex nests the failure under payload.error on an event_msg record.

    Regression: the message body has no error keyword and the machine code lives
    in payload.error.codex_error_info, so the session previously recorded as
    nonzero_exit_unclassified with error=None — hiding an arm that was simply
    at provider capacity.
    """
    traj = tmp_path / "rollout.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "x"}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "working on it"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {
                    "message": "Selected model is at capacity. Please try a different model.",
                    "codex_error_info": "server_overloaded",
                },
            },
        },
    ]
    traj.write_text(
        "".join(json.dumps(rec) + "\n" for rec in records),
        encoding="utf-8",
    )

    reason, err = capture_session_failure(
        exit_code=1,
        duration_seconds=443,
        input_tokens=3_695_970,
        trajectory_path=traj,
        harness_stderr_path=None,
    )

    assert reason == FAILURE_REASON_UPSTREAM_OVERLOADED
    assert err is not None
    assert "server_overloaded" in err
    assert "Selected model is at capacity" in err


def test_classify_overload_beats_loose_rate_limit_substring():
    """A capacity body that also says 'rate'/'limit' is not an account rate limit."""
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=30,
        input_tokens=100,
        has_assistant_turn=True,
        error_text=(
            "error: codex_error_info:server_overloaded; "
            "The model is at capacity due to rate limit constraints upstream"
        ),
    )
    assert reason == FAILURE_REASON_UPSTREAM_OVERLOADED


def test_structured_error_signals_extracts_codex_payload_error():
    """Pin the exact signal produced from a Codex payload.error record."""
    rec = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "error": {
                "message": "Selected model is at capacity. Please try a different model.",
                "codex_error_info": "server_overloaded",
            },
        },
    }
    assert _structured_error_signals(rec) == [
        "error: codex_error_info:server_overloaded; "
        "Selected model is at capacity. Please try a different model."
    ]


def test_structured_error_signals_keeps_overload_past_truncation():
    """An overload phrase beyond the stored cut still classifies as overload."""
    rec = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "error": {"message": ("x" * 600) + " model is at capacity"},
        },
    }
    signals = _structured_error_signals(rec)
    assert any(FAILURE_REASON_UPSTREAM_OVERLOADED in signal for signal in signals)
    # The marker alone must drive classification when the phrase itself was cut
    # — including after the extractor's 500-char cut of the joined signal.
    reason = classify_failure_reason(
        exit_code=1,
        duration_seconds=60,
        input_tokens=100,
        has_assistant_turn=True,
        error_text="; ".join(signals)[:500],
    )
    assert reason == FAILURE_REASON_UPSTREAM_OVERLOADED


def test_error_line_prefers_http_status_over_numeric_substring(tmp_path: Path):
    """A trailing numeric field containing "402" must not mask the real error.

    Grok trajectories end with usage metadata (``"costUsdTicks": 402322000``).
    The bare ``402`` alternation matched it and "last match wins" shadowed the
    earlier ``402 Payment Required`` line, so an infrastructure death
    classified as ``nonzero_exit_unclassified`` instead of quota — the judge
    then scored the pre-work death as a low but numeric reward.
    """
    traj = tmp_path / "grok.jsonl"
    traj.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": (
                    "HTTP/1.1 402 Payment Required): " "Grok Build usage balance exhausted"
                ),
            }
        )
        + "\n"
        + json.dumps({"type": "assistant", "message": '{"costUsdTicks": 402322000}'})
        + "\n",
        encoding="utf-8",
    )
    err = _extract_trajectory_error_line(traj)
    assert err is not None and "402 Payment Required" in err
    assert (
        classify_failure_reason(
            exit_code=1,
            duration_seconds=60,
            input_tokens=0,
            has_assistant_turn=True,
            error_text=err,
        )
        == FAILURE_REASON_QUOTA
    )


def test_error_line_regex_requires_status_code_boundaries():
    from gptme_sessions.failure_capture import _ERROR_LINE_RE

    # Numeric substrings of larger fields are not HTTP status codes.
    assert _ERROR_LINE_RE.search('"costUsdTicks": 402322000') is None
    assert _ERROR_LINE_RE.search('"http_status": 402') is not None
    # Decimal/comma-separated numeric fields are not status codes either
    # (\b does not treat '.' or ',' as word characters).
    assert _ERROR_LINE_RE.search("status: 402") is not None
    assert _ERROR_LINE_RE.search('"costUsd": 402.5') is None
    assert _ERROR_LINE_RE.search('"rate": 429,500') is None
    assert _ERROR_LINE_RE.search("HTTP/1.1 402 Payment Required") is not None
    # Alphanumeric adjacency is not a standalone code either.
    assert _ERROR_LINE_RE.search("402abc") is None
    assert _ERROR_LINE_RE.search("abc402") is None
    assert _ERROR_LINE_RE.search('"costUsdTicks402322000"') is None
    # Thousands-separated values are not codes.
    assert _ERROR_LINE_RE.search("1,402") is None
    # ...but a JSON value followed by a field comma IS a real code.
    assert _ERROR_LINE_RE.search('"http_status": 402,') is not None
    assert _ERROR_LINE_RE.search('{"http_status": 429, "quota": true}') is not None
    # A comma-separated list of statuses still matches.
    assert _ERROR_LINE_RE.search("401, 402") is not None
