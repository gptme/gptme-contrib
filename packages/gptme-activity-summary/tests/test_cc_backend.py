"""Tests for cc_backend module."""

import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from gptme_activity_summary.cc_backend import (
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


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_exhausted_raises_immediately(mock_run, mock_sleep):
    """Weekly quota exhaustion must raise ClaudeQuotaExhaustedError immediately.

    Retrying the same slot is futile when it is quota-exhausted; the failure
    should surface on the first attempt so a caller with slot fallback can retry
    on a different slot instead of burning the full retry window.
    """
    from gptme_activity_summary.cc_backend import ClaudeQuotaExhaustedError

    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit · resets 4pm (UTC)"
    )
    with pytest.raises(ClaudeQuotaExhaustedError) as exc_info:
        call_claude_code("test prompt", max_retries=3)
    assert exc_info.value.returncode == 1
    assert mock_run.call_count == 1  # no retries — quota failure is permanent
    assert mock_sleep.call_count == 0  # no backoff sleep burned


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_marker_in_stderr(mock_run, mock_sleep):
    """Quota marker in stderr (not stdout) must also be detected."""
    from gptme_activity_summary.cc_backend import ClaudeQuotaExhaustedError

    mock_run.return_value = _make_completed_process(
        returncode=1, stderr="You've hit your weekly limit"
    )
    with pytest.raises(ClaudeQuotaExhaustedError):
        call_claude_code("test prompt", max_retries=3)
    assert mock_run.call_count == 1


@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_is_called_process_error_subtype(mock_run, mock_sleep):
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


@patch.dict("os.environ", {}, clear=True)
@patch("subprocess.run")
def test_call_claude_code_cmd_prefix_empty_env_unchanged(mock_run):
    """No GPTME_CC_CMD_PREFIX => plain claude -p invocation."""
    mock_run.return_value = _make_completed_process(stdout='{"ok": true}')
    call_claude_code("test prompt")
    cmd = mock_run.call_args[0][0]
    assert cmd == ["claude", "-p", "-"]


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


@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_success(mock_run, mock_sleep, mock_fallback, tmp_path):
    """Quota exhaustion on primary slot triggers fallback; first healthy slot wins."""
    fb_cred = tmp_path / ".credentials.json.alice"
    fb_cred.write_text("{}")

    mock_run.return_value = _make_completed_process(
        returncode=1, stdout="You've hit your weekly limit · resets 4pm"
    )
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


@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_all_exhausted(
    mock_run, mock_sleep, mock_fallback, tmp_path
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

    import os

    prev = os.environ.pop("GPTME_CC_FALLBACK_CREDS", None)
    os.environ["GPTME_CC_FALLBACK_CREDS"] = str(fb_cred)
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


@patch("gptme_activity_summary.cc_backend._try_with_credential_file")
@patch("gptme_activity_summary.cc_backend.time.sleep")
@patch("subprocess.run")
def test_call_claude_code_quota_fallback_missing_file_skipped(mock_run, mock_sleep, mock_fallback):
    """A fallback cred path that does not exist on disk is silently skipped."""
    import os

    nonexistent = "/tmp/nonexistent-slot-cred-99999"
    prev = os.environ.pop("GPTME_CC_FALLBACK_CREDS", None)
    os.environ["GPTME_CC_FALLBACK_CREDS"] = nonexistent
    mock_run.return_value = _make_completed_process(returncode=1, stdout="weekly limit")
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
