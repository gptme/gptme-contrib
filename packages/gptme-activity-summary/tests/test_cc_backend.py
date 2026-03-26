"""Tests for cc_backend module."""

import subprocess
from unittest.mock import patch

from gptme_activity_summary.cc_backend import (
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


@patch("subprocess.run")
def test_call_claude_code_nonzero_exit_raises(mock_run):
    """Test non-zero exit code raises CalledProcessError immediately."""
    mock_run.return_value = _make_completed_process(returncode=1, stderr="error msg")
    try:
        call_claude_code("test prompt")
        assert False, "Should have raised CalledProcessError"
    except subprocess.CalledProcessError as e:
        assert e.returncode == 1


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
def test_call_claude_code_unsets_env_vars(mock_run):
    """Test that CLAUDECODE and CLAUDE_CODE_ENTRYPOINT are unset."""
    mock_run.return_value = _make_completed_process(stdout="ok")

    import os

    os.environ["CLAUDECODE"] = "1"
    os.environ["CLAUDE_CODE_ENTRYPOINT"] = "/usr/bin/claude"
    try:
        call_claude_code("test")
        env_used = mock_run.call_args.kwargs["env"]
        assert "CLAUDECODE" not in env_used
        assert "CLAUDE_CODE_ENTRYPOINT" not in env_used
    finally:
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)


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
