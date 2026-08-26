"""Tests for cc_backend module."""

import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from gptme_activity_summary.cc_backend import (
    ClaudeAuthExpiredError,
    ClaudeQuotaExhaustedError,
    call_claude_code,
    extract_json_from_response,
    summarize_journal_with_cc,
)


def test_extract_json_plain():
    """Test extracting plain JSON."""
    response = '{"key": "value", "list": [1, 2, 3]}'
    result = extract_json_from_response(response)
    assert result == {"key": "value", "list": [1, 2, 3]}


def test_extract_json_code_block():
    """Test extracting JSON from markdown code block."""
    response = """Here's the result:

```json
{"accomplishments": ["did thing 1", "did thing 2"]}
```

That's all."""
    result = extract_json_from_response(response)
    assert result["accomplishments"] == ["did thing 1", "did thing 2"]


def test_extract_json_code_block_no_lang():
    """Test extracting JSON from code block without language tag."""
    response = """```
{"key": "value"}
```"""
    result = extract_json_from_response(response)
    assert result == {"key": "value"}


def test_extract_json_embedded():
    """Test extracting JSON embedded in text."""
    response = 'The result is {"key": "value"} as requested.'
    result = extract_json_from_response(response)
    assert result == {"key": "value"}


def test_extract_json_empty_response():
    """Test handling empty response."""
    result = extract_json_from_response("")
    assert result == {}


def test_extract_json_no_json():
    """Test handling response with no JSON."""
    result = extract_json_from_response("This is just text with no JSON at all.")
    assert result == {}


def test_extract_json_invalid_json():
    """Test handling invalid JSON."""
    result = extract_json_from_response("{invalid: json}")
    assert result == {}


def test_extract_json_complex():
    """Test extracting complex JSON structure."""
    response = """```json
{
    "accomplishments": ["feature X done"],
    "decisions": [{"topic": "arch", "decision": "use Y", "rationale": "faster"}],
    "narrative": "Worked on feature X, decided to use Y for performance."
}
```"""
    result = extract_json_from_response(response)
    assert len(result["accomplishments"]) == 1
    assert result["decisions"][0]["topic"] == "arch"
    assert "feature X" in result["narrative"]


# --- Tests for call_claude_code retry/logging behavior ---


def _make_completed_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Helper to create a CompletedProcess mock."""
    return subprocess.CompletedProcess(
        args=["claude", "-p", "-"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_success_first_try(mock_run, mock_sleep):
    """Test successful response on first attempt."""
    mock_run.return_value = _make_completed_process(stdout='{"key": "value"}')
    result = call_claude_code("test prompt")
    assert result == '{"key": "value"}'
    assert mock_run.call_count == 1
    mock_sleep.assert_not_called()


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_empty_then_success(mock_run, mock_sleep):
    """Test retry after empty response, then success."""
    mock_run.side_effect = [
        _make_completed_process(stdout=""),  # first: empty
        _make_completed_process(stdout='{"ok": true}'),  # second: success
    ]
    result = call_claude_code("test prompt", max_retries=3)
    assert result == '{"ok": true}'
    assert mock_run.call_count == 2
    mock_sleep.assert_called_once()


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_all_empty_returns_empty(mock_run, mock_sleep):
    """Test all retries exhausted returns empty string."""
    mock_run.return_value = _make_completed_process(stdout="")
    result = call_claude_code("test prompt", max_retries=3)
    assert result == ""
    assert mock_run.call_count == 3
    assert mock_sleep.call_count == 2  # sleeps between attempts 1-2 and 2-3


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_all_empty_logs_error(mock_run, mock_sleep, caplog):
    """Test error is logged when all retries return empty."""
    import logging

    mock_run.return_value = _make_completed_process(stdout="")
    with caplog.at_level(logging.WARNING):
        call_claude_code("test prompt", max_retries=2)
    assert any("empty response" in msg.lower() for msg in caplog.messages)


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_whitespace_only_counts_as_empty(mock_run, mock_sleep):
    """Test that whitespace-only response is treated as empty."""
    mock_run.side_effect = [
        _make_completed_process(stdout="  \n  "),  # whitespace only → stripped to empty
        _make_completed_process(stdout='{"ok": true}'),
    ]
    result = call_claude_code("test prompt", max_retries=2)
    assert result == '{"ok": true}'
    assert mock_run.call_count == 2


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_nonzero_exit_raises_after_retries(mock_run, mock_sleep):
    """Test non-zero exit code raises CalledProcessError after exhausting retries."""
    mock_run.return_value = _make_completed_process(returncode=1, stderr="rate limited")
    try:
        call_claude_code("test prompt", max_retries=3)
        assert False, "Should have raised CalledProcessError"
    except subprocess.CalledProcessError as e:
        assert e.returncode == 1
    assert mock_run.call_count == 3  # retried 3 times, not raised immediately
    assert mock_sleep.call_count == 2  # slept between attempts


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_exhausted_raises_immediately(mock_run, mock_sleep, mock_gptme):
    """Weekly quota exhaustion must raise ClaudeQuotaExhaustedError immediately.

    Retrying the same slot is futile when it is quota-exhausted; the failure
    should surface on the first attempt so a caller with slot fallback can retry
    on a different slot instead of burning the full retry window. (The gptme
    fallback is attempted, but returns "" so the original error is re-raised.)
    """
    from gptme_activity_summary.cc_backend import ClaudeQuotaExhaustedError

    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit · resets 4pm (UTC)"
    )
    with pytest.raises(ClaudeQuotaExhaustedError) as exc_info:
        call_claude_code("test prompt", max_retries=3)
    assert exc_info.value.returncode == 1
    mock_gptme.assert_called_once_with("test prompt", timeout=120)
    assert mock_run.call_count == 1  # primary tried once; no retry-loop
    assert mock_sleep.call_count == 0  # no backoff sleep burned


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_marker_in_stderr(mock_run, mock_sleep, mock_gptme):
    """Quota marker in stderr (not stdout) must also be detected."""
    from gptme_activity_summary.cc_backend import ClaudeQuotaExhaustedError

    mock_run.return_value = _make_completed_process(
        returncode=1, stderr="You've hit your weekly limit"
    )
    with pytest.raises(ClaudeQuotaExhaustedError):
        call_claude_code("test prompt", max_retries=3)
    assert mock_run.call_count == 1  # primary only; gptme mocked, no extra run


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_is_called_process_error_subtype(mock_run, mock_sleep, mock_gptme):
    """ClaudeQuotaExhaustedError must be catchable as CalledProcessError."""
    from gptme_activity_summary.cc_backend import ClaudeQuotaExhaustedError

    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    try:
        call_claude_code("test prompt", max_retries=3)
        assert False, "Should have raised ClaudeQuotaExhaustedError"
    except subprocess.CalledProcessError as e:
        assert isinstance(e, ClaudeQuotaExhaustedError)
    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@patch.dict(
    "os.environ",
    # Explicitly clear CLAUDECODE so nested detection doesn't append
    # --no-session-persistence and complicate the cmd assertion.
    {"GPTME_CC_CMD_PREFIX": "/opt/bin/slot-wrap --slot alice --", "CLAUDECODE": ""},
    clear=False,
)
@patch("subprocess.run")
def test_call_claude_code_cmd_prefix_env(mock_run):
    """GPTME_CC_CMD_PREFIX must prepend the claude command via shlex split."""
    mock_run.return_value = _make_completed_process(stdout='{"ok": true}')
    result = call_claude_code("test prompt")
    assert result == '{"ok": true}'
    cmd = mock_run.call_args[0][0]
    assert cmd[:5] == ["/opt/bin/slot-wrap", "--slot", "alice", "--", "claude"]
    assert cmd[5:7] == ["-p", "-"]
    assert "GPTME_CC_CMD_PREFIX" not in mock_run.call_args.kwargs["env"]


@patch.dict("os.environ", {}, clear=True)
@patch("subprocess.run")
def test_call_claude_code_cmd_prefix_empty_env_unchanged(mock_run):
    """No GPTME_CC_CMD_PREFIX => plain claude -p invocation."""
    mock_run.return_value = _make_completed_process(stdout='{"ok": true}')
    call_claude_code("test prompt")
    cmd = mock_run.call_args[0][0]
    assert cmd == ["claude", "-p", "-"]


@patch.dict("os.environ", {"GPTME_CC_CMD_PREFIX": "wrapper '"}, clear=True)
@patch("subprocess.run")
def test_call_claude_code_cmd_prefix_invalid_quote(mock_run):
    """An invalid command prefix reports which setting is malformed."""
    with pytest.raises(ValueError, match="Invalid GPTME_CC_CMD_PREFIX"):
        call_claude_code("test prompt")
    mock_run.assert_not_called()


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_does_not_match_generic_weekly_limit(mock_run, mock_sleep):
    """Unrelated output mentioning a weekly limit follows the normal retry path."""
    mock_run.side_effect = [
        _make_completed_process(returncode=1, stderr="See the weekly limit documentation"),
        _make_completed_process(stdout='{"ok": true}'),
    ]

    assert call_claude_code("test prompt", max_retries=2) == '{"ok": true}'
    assert mock_run.call_count == 2
    mock_sleep.assert_called_once()


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_nonzero_then_success(mock_run, mock_sleep):
    """Test retry after non-zero exit eventually succeeds."""
    mock_run.side_effect = [
        _make_completed_process(returncode=1, stderr="transient error"),
        _make_completed_process(stdout='{"ok": true}'),
    ]
    result = call_claude_code("test prompt", max_retries=3)
    assert result == '{"ok": true}'
    assert mock_run.call_count == 2
    mock_sleep.assert_called_once()


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_nonzero_logs_stderr(mock_run, mock_sleep, caplog):
    """Test that stderr is logged on non-zero exit."""
    import logging

    mock_run.return_value = _make_completed_process(returncode=1, stderr="quota exhausted")
    with caplog.at_level(logging.WARNING):
        try:
            call_claude_code("test prompt", max_retries=1)
        except subprocess.CalledProcessError:
            pass
    assert any("quota exhausted" in msg for msg in caplog.messages)


@patch("gptme_activity_summary.cc_backend.datetime")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_nonzero_logs_stdout_and_debug_file(
    mock_run, mock_sleep, mock_datetime, caplog, tmp_path
):
    """Test failures preserve stdout and trace the retry in a debug log."""
    import logging

    mock_datetime.now.return_value = datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc)
    debug_file = tmp_path / "claude-20260715T010203.000000Z-attempt-2.log"
    mock_run.side_effect = [
        _make_completed_process(returncode=1, stdout="Your session quota is exhausted"),
        _make_completed_process(returncode=1, stdout="Your session quota is exhausted"),
    ]
    with caplog.at_level(logging.WARNING):
        try:
            call_claude_code(
                "test prompt",
                max_retries=2,
                diagnostic_dir=tmp_path,
            )
        except subprocess.CalledProcessError as error:
            assert error.output == "Your session quota is exhausted"
        else:
            assert False, "Should have raised CalledProcessError"

    log_text = "\n".join(caplog.messages)
    assert "stdout: Your session quota is exhausted" in log_text
    assert f"debug_file: {debug_file}" in log_text
    first_cmd = mock_run.call_args_list[0].args[0]
    retry_cmd = mock_run.call_args_list[1].args[0]
    assert "--debug-file" not in first_cmd
    assert retry_cmd[-2:] == ["--debug-file", str(debug_file)]


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_unsupported_debug_file_retries_plain(mock_run, mock_sleep, tmp_path):
    """An unsupported diagnostic flag must not consume the last plain retry."""
    mock_run.side_effect = [
        _make_completed_process(returncode=1, stderr="transient API failure"),
        _make_completed_process(returncode=1, stderr="unknown option --debug-file"),
        _make_completed_process(stdout='{"ok": true}'),
    ]

    result = call_claude_code("test prompt", max_retries=2, diagnostic_dir=tmp_path)

    assert result == '{"ok": true}'
    assert mock_run.call_count == 3
    assert "--debug-file" in mock_run.call_args_list[1].args[0]
    assert "--debug-file" not in mock_run.call_args_list[2].args[0]


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_home_resolution_failure_still_calls_claude(mock_run, mock_sleep):
    """Missing home-directory metadata must not block Claude invocation."""
    from unittest.mock import patch

    mock_run.return_value = _make_completed_process(stdout='{"ok": true}')

    with patch("gptme_activity_summary.cc_backend.Path.home", side_effect=RuntimeError("no home")):
        result = call_claude_code("test prompt")

    assert result == '{"ok": true}'
    assert "--debug-file" not in mock_run.call_args.args[0]


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_diagnostic_dir_mkdir_failure_still_retries(mock_run, mock_sleep):
    """Diagnostic dir mkdir failure must not block a Claude retry."""
    from pathlib import Path
    from unittest.mock import patch

    mock_run.side_effect = [
        _make_completed_process(returncode=1),
        _make_completed_process(stdout='{"ok": true}'),
    ]

    with patch.object(Path, "mkdir", side_effect=OSError("read-only filesystem")):
        result = call_claude_code("test prompt", max_retries=2)

    assert result == '{"ok": true}'
    assert mock_run.call_count == 2
    assert "--debug-file" not in mock_run.call_args_list[1].args[0]


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_linear_backoff(mock_run, mock_sleep):
    """Test that sleep uses linear backoff."""
    mock_run.return_value = _make_completed_process(stdout="")
    call_claude_code("test", max_retries=3)
    # Should sleep 5s after attempt 1, 10s after attempt 2
    sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
    assert sleep_calls == [5, 10]


@patch("subprocess.run")
def test_call_claude_code_unsets_all_cc_env_vars(mock_run):
    """Test that all CC-related env vars are stripped from subprocess."""
    mock_run.return_value = _make_completed_process(stdout="ok")

    import os

    cc_vars = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "/usr/bin/claude",
        "CC_SESSION_ID": "test-session-id",
        "CC_MODEL": "opus",
    }
    for k, v in cc_vars.items():
        os.environ[k] = v
    try:
        call_claude_code("test")
        env_used = mock_run.call_args.kwargs["env"]
        for var in cc_vars:
            assert var not in env_used, f"CC env var {var} should be stripped"
    finally:
        for k in cc_vars:
            os.environ.pop(k, None)


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_no_session_persistence_when_nested(mock_run, mock_sleep):
    """--no-session-persistence is passed only when CLAUDECODE is set (nested)."""
    import os

    mock_run.return_value = _make_completed_process(stdout="test output")

    # Nested case: CLAUDECODE set → flag present as belt-and-suspenders safeguard
    os.environ["CLAUDECODE"] = "1"
    try:
        call_claude_code("test prompt")
        cmd = mock_run.call_args[0][0]
        assert "--no-session-persistence" in cmd, (
            "Must pass --no-session-persistence when nested (CLAUDECODE set) "
            "to prevent empty-output bug (gptme/gptme-contrib#585)"
        )
    finally:
        os.environ.pop("CLAUDECODE", None)


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_no_flag_when_not_nested(mock_run, mock_sleep):
    """--no-session-persistence is dropped for non-nested calls so CC writes a full trajectory."""
    import os

    mock_run.return_value = _make_completed_process(stdout="test output")

    # Ensure CLAUDECODE is not set
    prev = os.environ.pop("CLAUDECODE", None)
    try:
        call_claude_code("test prompt")
        cmd = mock_run.call_args[0][0]
        assert "--no-session-persistence" not in cmd, (
            "Non-nested calls should NOT pass --no-session-persistence; "
            "dropping the flag lets CC write a full trajectory to ~/.claude/projects/. "
            "See ErikBjare/bob#681."
        )
    finally:
        if prev is not None:
            os.environ["CLAUDECODE"] = prev


# --- Tests for _cc_failed flag propagation ---


@patch("gptme_activity_summary.cc_backend.call_claude_code")
def test_summarize_journal_cc_failed_flag(mock_cc):
    """Test that _cc_failed is set when CC returns empty."""
    mock_cc.return_value = ""
    result = summarize_journal_with_cc("test content", "2026-03-26")
    assert result["_cc_failed"] is True
    assert result["narrative"] == ""
    assert result["accomplishments"] == []


@patch("gptme_activity_summary.cc_backend.call_claude_code")
def test_summarize_journal_no_failed_flag_on_success(mock_cc):
    """Test that _cc_failed is NOT set when CC returns valid JSON."""
    mock_cc.return_value = '{"narrative": "did stuff", "accomplishments": ["thing"]}'
    result = summarize_journal_with_cc("test content", "2026-03-26")
    assert "_cc_failed" not in result
    assert result["narrative"] == "did stuff"


# --- Tests for GPTME_CC_FALLBACK_CREDS slot fallback ---


@pytest.mark.parametrize(
    "primary_failure",
    [
        "You've hit your weekly limit · resets 4pm",
        (
            "Your organization has disabled Claude subscription access for Claude Code · "
            "Use an Anthropic API key instead, or ask your admin to enable access"
        ),
        "Failed to authenticate: OAuth session expired",
    ],
)
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_slot_failure_fallback_success(
    mock_run, mock_sleep, mock_fallback, tmp_path, primary_failure
):
    """Permanent subscription failures trigger fallback; first healthy slot wins."""
    fb_cred = tmp_path / ".credentials.json.alice"
    fb_cred.write_text("{}")

    mock_run.return_value = _make_completed_process(returncode=1, stdout=primary_failure)
    mock_fallback.return_value = _make_completed_process(stdout='{"ok": true}')

    import os

    prev = os.environ.pop("GPTME_CC_FALLBACK_CREDS", None)
    os.environ["GPTME_CC_FALLBACK_CREDS"] = str(fb_cred)
    try:
        result = call_claude_code("test prompt")
    finally:
        if prev is None:
            os.environ.pop("GPTME_CC_FALLBACK_CREDS", None)
        else:
            os.environ["GPTME_CC_FALLBACK_CREDS"] = prev

    assert result == '{"ok": true}'
    assert mock_run.call_count == 1  # primary slot tried once
    assert mock_fallback.call_count == 1  # fallback tried once


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("claude", 30), OSError("boom")])
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_continues_after_launch_failure(
    mock_run, mock_sleep, mock_fallback, tmp_path, failure
):
    """A hung or unlaunchable fallback slot does not block later slots."""
    first_cred = tmp_path / ".credentials.json.first"
    second_cred = tmp_path / ".credentials.json.second"
    first_cred.write_text("{}")
    second_cred.write_text("{}")
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_fallback.side_effect = [failure, _make_completed_process(stdout='{"ok": true}')]

    with patch.dict(
        "os.environ",
        {"GPTME_CC_FALLBACK_CREDS": f"{first_cred}:{second_cred}"},
        clear=True,
    ):
        assert call_claude_code("test prompt") == '{"ok": true}'

    assert mock_fallback.call_count == 2


@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_retries_empty_response(
    mock_run, mock_sleep, mock_fallback, tmp_path
):
    """A transient empty response from a healthy fallback slot is retried."""
    fb_cred = tmp_path / ".credentials.json.alice"
    fb_cred.write_text("{}")
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_fallback.side_effect = [
        _make_completed_process(stdout=""),
        _make_completed_process(stdout='{"ok": true}'),
    ]

    with patch.dict(
        "os.environ",
        {"GPTME_CC_FALLBACK_CREDS": str(fb_cred)},
        clear=True,
    ):
        assert call_claude_code("test prompt", max_retries=2) == '{"ok": true}'

    assert mock_fallback.call_count == 2
    mock_sleep.assert_called_once()


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_empty_exhaustion_returns_empty(
    mock_run, mock_sleep, mock_fallback, mock_gptme, tmp_path
):
    """Repeated empty responses retain the main path's graceful-empty contract."""
    fb_cred = tmp_path / ".credentials.json.alice"
    fb_cred.write_text("{}")
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_fallback.return_value = _make_completed_process(stdout="")

    with patch.dict(
        "os.environ",
        {"GPTME_CC_FALLBACK_CREDS": str(fb_cred)},
        clear=True,
    ):
        assert call_claude_code("test prompt", max_retries=2) == ""

    assert mock_fallback.call_count == 2
    mock_sleep.assert_called_once()
    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_all_exhausted(
    mock_run, mock_sleep, mock_fallback, mock_gptme, tmp_path
):
    """If all fallback slots are also exhausted, raise ClaudeQuotaExhaustedError."""
    fb_cred = tmp_path / ".credentials.json.erik"
    fb_cred.write_text("{}")

    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_fallback.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )

    with patch.dict(
        "os.environ",
        {"GPTME_CC_FALLBACK_CREDS": str(fb_cred)},
        clear=True,
    ):
        with pytest.raises(ClaudeQuotaExhaustedError):
            call_claude_code("test prompt")
    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_oauth_expiry_raises_auth_error(
    mock_run, mock_sleep, mock_fallback, mock_gptme
):
    """OAuth session expiry raises ClaudeAuthExpiredError (a subtype of ClaudeQuotaExhaustedError).

    ClaudeAuthExpiredError indicates a recoverable auth failure (re-auth via /login)
    rather than a permanent quota exhaustion, while still inheriting from
    ClaudeQuotaExhaustedError so existing callers that catch the base type keep working.
    """
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="Failed to authenticate: OAuth session expired"
    )
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ClaudeAuthExpiredError) as exc_info:
            call_claude_code("test prompt")
    assert isinstance(exc_info.value, ClaudeQuotaExhaustedError)


@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_retries_non_quota_error(
    mock_run, mock_sleep, mock_fallback, tmp_path
):
    """A transient fallback error gets the same retry opportunity as the primary slot."""
    fb_cred = tmp_path / ".credentials.json.alice"
    fb_cred.write_text("{}")
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_fallback.side_effect = [
        _make_completed_process(returncode=1, stderr="temporary API failure"),
        _make_completed_process(stdout='{"ok": true}'),
    ]

    with patch.dict(
        "os.environ",
        {"GPTME_CC_FALLBACK_CREDS": str(fb_cred)},
        clear=True,
    ):
        assert call_claude_code("test prompt", max_retries=2) == '{"ok": true}'

    assert mock_fallback.call_count == 2
    mock_sleep.assert_called_once_with(5)


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_preserves_non_quota_error(
    mock_run, mock_sleep, mock_fallback, mock_gptme, tmp_path
):
    """An exhausted fallback retry window preserves the last non-quota failure.

    gptme fallback is attempted first (and produces nothing), then the
    non-subscription error from the credential slot is re-raised.
    """
    fb_cred = tmp_path / ".credentials.json.invalid"
    fb_cred.write_text("{}")
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_fallback.return_value = _make_completed_process(returncode=2, stderr="invalid credentials")

    with patch.dict(
        "os.environ",
        {"GPTME_CC_FALLBACK_CREDS": str(fb_cred)},
        clear=True,
    ):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            call_claude_code("test prompt", max_retries=2)

    assert not isinstance(exc_info.value, ClaudeQuotaExhaustedError)
    assert exc_info.value.returncode == 2
    assert exc_info.value.stderr == "invalid credentials"
    assert mock_fallback.call_count == 2
    mock_sleep.assert_called_once_with(5)
    mock_gptme.assert_called_once()  # gptme is tried before raising


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_non_quota_then_quota_raises_quota_error(
    mock_run, mock_sleep, mock_fallback, mock_gptme, tmp_path
):
    """Non-quota error on slot A then quota-exhaustion on slot B → ClaudeQuotaExhaustedError.

    Regression for: last_non_quota_error was not cleared when a later fallback slot
    was itself quota-exhausted, causing the stale error from slot A to be raised instead
    of ClaudeQuotaExhaustedError — violating the stated contract.
    """
    cred_a = tmp_path / ".credentials.json.alice"
    cred_a.write_text("{}")
    cred_b = tmp_path / ".credentials.json.erik"
    cred_b.write_text("{}")

    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_fallback.side_effect = [
        # Slot A: non-quota failure (e.g. invalid credentials)
        _make_completed_process(returncode=2, stderr="invalid credentials"),
        # Slot B: also quota-exhausted
        _make_completed_process(returncode=1, stdout="You've hit your weekly limit"),
    ]

    with patch.dict(
        "os.environ",
        {"GPTME_CC_FALLBACK_CREDS": f"{cred_a}:{cred_b}"},
        clear=True,
    ):
        with pytest.raises(ClaudeQuotaExhaustedError):
            call_claude_code("test prompt", max_retries=1)
    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_missing_file_skipped(
    mock_run, mock_sleep, mock_fallback, mock_gptme
):
    """A fallback cred path that does not exist on disk is silently skipped."""
    import os

    nonexistent = "/tmp/nonexistent-slot-cred-99999"
    prev = os.environ.pop("GPTME_CC_FALLBACK_CREDS", None)
    os.environ["GPTME_CC_FALLBACK_CREDS"] = nonexistent
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    try:
        try:
            call_claude_code("test prompt")
            assert False, "Should have raised ClaudeQuotaExhaustedError"
        except ClaudeQuotaExhaustedError:
            pass
    finally:
        if prev is None:
            os.environ.pop("GPTME_CC_FALLBACK_CREDS", None)
        else:
            os.environ["GPTME_CC_FALLBACK_CREDS"] = prev

    mock_fallback.assert_not_called()  # missing file never attempted
    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@patch.dict("os.environ", {}, clear=True)
@patch("subprocess.run")
def test_call_claude_code_fallback_creds_not_passed_to_subprocess(mock_run):
    """GPTME_CC_FALLBACK_CREDS must be stripped from the env passed to subprocess."""
    import os

    os.environ["GPTME_CC_FALLBACK_CREDS"] = "/tmp/fake-cred"
    mock_run.return_value = _make_completed_process(stdout="ok")
    call_claude_code("test prompt")
    subprocess_env = mock_run.call_args.kwargs["env"]
    assert "GPTME_CC_FALLBACK_CREDS" not in subprocess_env


# --- Tests for gptme fallback on quota exhaustion ---


def _ndjson(msg: str) -> str:
    """Build a minimal gptme NDJSON stream containing one assistant message."""
    import json as _json

    return _json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": msg,
            "timestamp": "2026-08-23T00:00:00.000000",
        }
    )


@pytest.mark.parametrize(
    "gptme_response",
    [
        '{"narrative": "from-gptme"}',
        '{"month_narrative": "from-gptme"}',
    ],
)
@patch.dict("os.environ", {}, clear=True)
@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_falls_back_to_gptme(
    mock_run, mock_sleep, mock_gptme, gptme_response
):
    """Quota exhaustion on all Claude slots falls back to the gptme backend."""
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_gptme.return_value = gptme_response

    result = call_claude_code("test prompt", max_retries=3)

    assert result == gptme_response
    assert mock_gptme.call_count == 1
    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@pytest.mark.parametrize(
    "fallback_response",
    [
        "I cannot provide JSON.",
        '{"error": "model overloaded"}',
    ],
)
@patch.dict("os.environ", {}, clear=True)
@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_rejects_invalid_gptme_fallback(
    mock_run, mock_sleep, mock_gptme, fallback_response
):
    """Non-summary gptme responses must preserve the Claude failure signal."""
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_gptme.return_value = fallback_response

    with pytest.raises(ClaudeQuotaExhaustedError):
        call_claude_code("test prompt", max_retries=1)

    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_disabled_fallback_slots_reach_gptme(
    mock_run, mock_sleep, mock_fallback, mock_gptme, tmp_path
):
    """Subscription-disabled fallback slots still fall through to gptme."""
    import os

    fb_cred = tmp_path / ".credentials.json.alice"
    fb_cred.write_text("{}")
    disabled = _make_completed_process(
        returncode=1,
        stdout="Your organization has disabled Claude subscription access for Claude Code",
    )
    mock_run.return_value = disabled
    mock_fallback.return_value = disabled
    mock_gptme.return_value = '{"narrative": "from-gptme"}'

    with patch.dict(
        os.environ,
        {"GPTME_CC_FALLBACK_CREDS": str(fb_cred)},
        clear=True,
    ):
        result = call_claude_code("test prompt")

    assert result == '{"narrative": "from-gptme"}'
    mock_fallback.assert_called_once()
    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@patch.dict("os.environ", {}, clear=True)
@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_gptme_failure_raises_original(mock_run, mock_sleep, mock_gptme):
    """If the gptme fallback yields nothing, the original quota error is raised."""
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_gptme.return_value = ""  # fallback failed

    with pytest.raises(ClaudeQuotaExhaustedError):
        call_claude_code("test prompt", max_retries=3)
    mock_gptme.assert_called_once_with("test prompt", timeout=120)


@patch.dict("os.environ", {}, clear=True)
@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_gptme_fallback_passes_timeout(mock_run, mock_sleep, mock_gptme):
    """The gptme fallback inherits the caller's timeout."""
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    mock_gptme.return_value = '{"narrative": "ok"}'
    call_claude_code("test prompt", timeout=99, max_retries=1)
    mock_gptme.assert_called_once_with("test prompt", timeout=99)


@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_auth_expiry_raised_when_fallback_slot_returns_empty(
    mock_run, mock_sleep, mock_fallback, mock_gptme, tmp_path
):
    """Auth-expiry from primary must surface even when fallback slot returns empty."""
    import os

    fb_cred = tmp_path / ".credentials.json.alice"
    fb_cred.write_text("{}")
    # Primary fails with auth expiry marker.
    auth_error = _make_completed_process(
        returncode=1,
        stdout="Failed to authenticate: OAuth session expired",
    )
    mock_run.return_value = auth_error
    # Fallback credential slot returns rc=0 but empty stdout.
    mock_fallback.return_value = _make_completed_process(returncode=0, stdout="")
    # gptme fallback also yields nothing.
    mock_gptme.return_value = ""

    with patch.dict(
        os.environ,
        {"GPTME_CC_FALLBACK_CREDS": str(fb_cred)},
        clear=True,
    ):
        with pytest.raises(ClaudeAuthExpiredError):
            call_claude_code("test prompt", max_retries=1)


# --- Tests for gptme_backend module ---


def test_call_gptme_disabled_by_default():
    """Fallback is off by default (no env var set); does not spawn the binary."""
    import gptme_activity_summary.gptme_backend as gb

    with patch.dict("os.environ", {}, clear=True):
        with patch.object(gb, "shutil") as mock_shutil:
            mock_shutil.which.return_value = "/usr/bin/gptme"
            with patch("subprocess.run") as mock_run:
                assert gb.call_gptme("hi") == ""
    mock_run.assert_not_called()


def test_call_gptme_disabled_by_env():
    """Explicitly disabling via env (=0) short-circuits and does not spawn the binary."""
    import gptme_activity_summary.gptme_backend as gb

    with patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "0"}, clear=True):
        with patch.object(gb, "shutil") as mock_shutil:
            mock_shutil.which.return_value = "/usr/bin/gptme"
            with patch("subprocess.run") as mock_run:
                assert gb.call_gptme("hi") == ""
    mock_run.assert_not_called()


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value=None)
def test_call_gptme_missing_binary(mock_which):
    """Missing gptme binary is handled gracefully (fallback enabled but binary absent)."""
    import gptme_activity_summary.gptme_backend as gb

    assert gb.call_gptme("hi") == ""


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme")
@patch("subprocess.run")
def test_call_gptme_extracts_assistant_text(mock_run, mock_which):
    """NDJSON assistant content is extracted from the gptme output (str form)."""
    import gptme_activity_summary.gptme_backend as gb

    mock_run.return_value = subprocess.CompletedProcess(
        args=["gptme"], returncode=0, stdout=_ndjson('{"result": "ok"}')
    )
    assert gb.call_gptme("hi") == '{"result": "ok"}'


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme")
@patch("subprocess.run")
def test_call_gptme_extracts_assistant_text_list_content(mock_run, mock_which):
    """NDJSON assistant content is extracted when content is a list of parts (real gptme format)."""
    import json as _json

    import gptme_activity_summary.gptme_backend as gb

    ndjson = _json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": '{"result": "ok"}'}],
            "timestamp": "2026-08-23T00:00:00.000000",
        }
    )
    mock_run.return_value = subprocess.CompletedProcess(args=["gptme"], returncode=0, stdout=ndjson)
    assert gb.call_gptme("hi") == '{"result": "ok"}'


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme")
@patch("subprocess.run")
def test_call_gptme_nonzero_exit_returns_empty(mock_run, mock_which):
    """Non-zero gptme exit returns an empty string (no raise)."""
    import gptme_activity_summary.gptme_backend as gb

    mock_run.return_value = subprocess.CompletedProcess(
        args=["gptme"], returncode=1, stdout="", stderr="boom"
    )
    assert gb.call_gptme("hi") == ""


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme")
@patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gptme", 30))
def test_call_gptme_timeout_returns_empty(mock_run, mock_which):
    """A timed-out gptme call returns an empty string (no raise)."""
    import gptme_activity_summary.gptme_backend as gb

    assert gb.call_gptme("hi") == ""


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme")
@patch("subprocess.run")
def test_call_gptme_prompt_passed_via_stdin(mock_run, mock_which):
    """Prompt is passed as stdin input, not as a positional CLI argument."""
    import gptme_activity_summary.gptme_backend as gb

    mock_run.return_value = subprocess.CompletedProcess(
        args=["gptme"], returncode=0, stdout=_ndjson("result")
    )
    gb.call_gptme("my secret prompt")

    _, kwargs = mock_run.call_args
    # prompt must not appear in the command list
    assert "my secret prompt" not in mock_run.call_args[0][0]
    # prompt must be passed via the input kwarg
    assert kwargs.get("input") == "my secret prompt"


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme")
@patch("subprocess.run")
def test_call_gptme_list_content_non_string_text_skipped(mock_run, mock_which):
    """Non-string text values in list content parts are skipped without raising."""
    import json as _json

    import gptme_activity_summary.gptme_backend as gb

    # Emit a part with text=None (edge case in malformed gptme output)
    ndjson = _json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": None},
                {"type": "text", "text": "valid text"},
            ],
        }
    )
    mock_run.return_value = subprocess.CompletedProcess(args=["gptme"], returncode=0, stdout=ndjson)
    # Must not raise AttributeError; must return the valid part
    assert gb.call_gptme("hi") == "valid text"


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme")
@patch("subprocess.run")
def test_call_gptme_multiple_assistant_messages_returns_first_json(mock_run, mock_which):
    """When gptme emits multiple assistant messages, the first JSON-parseable one is returned.

    Reasoning models emit a thinking/preamble message first, followed by the
    real JSON answer.  Joining all messages with newlines produces a multi-JSON
    string — the greedy regex in extract_json_from_response then spans across
    both objects and fails to parse any valid JSON, so we iterate instead and
    return the first message that parses as JSON.
    """
    import json as _json

    import gptme_activity_summary.gptme_backend as gb

    first_msg = _json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": '{"narrative": "first"}',
            "timestamp": "2026-08-23T00:00:00.000000",
        }
    )
    second_msg = _json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": "No tool call detected.",
            "timestamp": "2026-08-23T00:00:01.000000",
        }
    )
    mock_run.return_value = subprocess.CompletedProcess(
        args=["gptme"], returncode=0, stdout=f"{first_msg}\n{second_msg}"
    )
    result = gb.call_gptme("hi")
    assert result == '{"narrative": "first"}', "first JSON message must be returned"


@patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"})
@patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme")
@patch("subprocess.run")
def test_call_gptme_reasoning_model_preamble_skipped(mock_run, mock_which):
    """Reasoning models (e.g. deepseek) emit a thinking preamble before the JSON answer.

    The first assistant message is not valid JSON; the second contains the real
    JSON summary.  _extract_assistant_text must skip the non-JSON preamble and
    return the first message that parses as JSON.
    """
    import json as _json

    import gptme_activity_summary.gptme_backend as gb

    preamble_msg = _json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": "Let me think about this... I need to summarize the journal entries.",
            "timestamp": "2026-08-23T00:00:00.000000",
        }
    )
    json_answer_msg = _json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": '{"narrative": "actual summary", "title": "Daily Summary"}',
            "timestamp": "2026-08-23T00:00:01.000000",
        }
    )
    mock_run.return_value = subprocess.CompletedProcess(
        args=["gptme"], returncode=0, stdout=f"{preamble_msg}\n{json_answer_msg}"
    )
    result = gb.call_gptme("summarize")
    assert (
        result == '{"narrative": "actual summary", "title": "Daily Summary"}'
    ), "must skip non-JSON preamble and return the JSON answer message"


@patch.dict("os.environ", {}, clear=True)
@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_gptme_fallback_remaps_alternate_narrative_key(
    mock_run, mock_sleep, mock_gptme
):
    """When gptme returns an alternate recognised key instead of the exact requested one,
    the key is remapped so the caller always finds narrative_key in the returned JSON.

    Without remapping, a monthly summary call (narrative_key='month_narrative') that
    receives '{"narrative": "..."}' passes schema validation but leaves the caller
    reading gptme_result.get('month_narrative', '') — which defaults to '' and
    silently produces an empty narrative on quota days.
    """
    import json as _json

    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    # gptme returns "narrative" but the caller expects "month_narrative"
    mock_gptme.return_value = '{"narrative": "monthly stuff"}'

    result = call_claude_code("test prompt", max_retries=1, narrative_key="month_narrative")

    parsed = _json.loads(result)
    assert "month_narrative" in parsed, "caller-expected key must be present after remap"
    assert parsed["month_narrative"] == "monthly stuff"
    assert "narrative" not in parsed, "original alternate key must be replaced by remap"


@patch.dict("os.environ", {}, clear=True)
@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_gptme_fallback_remaps_when_requested_key_empty(
    mock_run, mock_sleep, mock_gptme
):
    """When gptme returns JSON with the requested key present but empty, the remap
    must treat the empty value as absent and use the alternate key.

    Without this fix, narrative_key='month_narrative' and gptme returning
    '{"month_narrative": "", "narrative": "monthly text"}' passes the
    `narrative_key not in gptme_result` check (key exists), so no remap happens.
    The caller then reads month_narrative and gets '', producing a silent empty
    monthly summary on quota days — the exact failure the remap was introduced to prevent.
    """
    import json as _json

    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    # gptme returns month_narrative as empty string — alternate key has the real value
    mock_gptme.return_value = '{"month_narrative": "", "narrative": "actual monthly narrative"}'

    result = call_claude_code("test prompt", max_retries=1, narrative_key="month_narrative")

    parsed = _json.loads(result)
    assert "month_narrative" in parsed, "caller-expected key must be present after remap"
    assert (
        parsed["month_narrative"] == "actual monthly narrative"
    ), "empty requested key must be replaced with the alternate key's value"


@patch.dict("os.environ", {}, clear=True)
@patch("gptme_activity_summary.cc_backend.call_gptme")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_gptme_fallback_rejects_empty_requested_key_no_alternate(
    mock_run, mock_sleep, mock_gptme
):
    """When gptme returns the requested key present but empty and no alternate key
    has content, the fallback must be rejected — not returned as a 'successful'
    empty summary.

    Regression: the remap loop previously did a no-op self-assignment
    (gptme_result[k] = gptme_result.pop(k)) when alt_key == narrative_key, then
    the validation at line 360 used `key in gptme_result` (existence) rather than
    `gptme_result.get(key)` (content), so an empty month_narrative was accepted and
    returned as a successful fallback — producing a silent empty narrative on quota days.
    """
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit"
    )
    # gptme returns the requested key but empty — no alternate key present
    mock_gptme.return_value = '{"month_narrative": ""}'

    with pytest.raises(ClaudeQuotaExhaustedError):
        call_claude_code("test prompt", max_retries=1, narrative_key="month_narrative")


@patch("gptme_activity_summary.cc_backend.call_gptme", return_value="")
@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_oauth_expiry_preserved_when_fallback_has_non_sub_error(
    mock_run, mock_sleep, mock_fallback, mock_gptme, tmp_path
):
    """Auth-expiry from the primary slot is raised even when a fallback slot produces a
    non-subscription error (last_non_subscription_error is set).

    Regression: previously ClaudeAuthExpiredError was only raised on the
    last_non_subscription_error is None path, so a stale-auth fallback slot
    could mask the primary's OAuth expiry with a generic CalledProcessError.
    """
    fb_cred = tmp_path / ".credentials.json.invalid"
    fb_cred.write_text("{}")
    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="Failed to authenticate: OAuth session expired"
    )
    mock_fallback.return_value = _make_completed_process(returncode=2, stderr="invalid credentials")

    with patch.dict(
        "os.environ",
        {"GPTME_CC_FALLBACK_CREDS": str(fb_cred)},
        clear=True,
    ):
        with pytest.raises(ClaudeAuthExpiredError):
            call_claude_code("test prompt", max_retries=2)
