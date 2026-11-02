"""Billing-disabled Claude must reach the existing, opt-in backend fallback."""

import json
import subprocess
from unittest.mock import patch

import pytest

from gptme_activity_summary.cc_backend import ClaudeQuotaExhaustedError, call_claude_code

BILLING_FAILURE = (
    "Claude Code can't use your Claude subscription, usually because a payment is past due "
    "· Pay or renew at https://claude.ai/settings/billing (if you just paid, try again shortly), "
    "or use an Anthropic API key instead"
)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("narrative_key", ["narrative", "month_narrative"])
def test_billing_failure_reaches_enabled_backend(stream, narrative_key, tmp_path):
    """Exercise both real adapters; replace only process execution/discovery."""
    answer = json.dumps({narrative_key: "Recovered summary"})
    failure = subprocess.CompletedProcess(
        ["claude"], 1, **{stream: BILLING_FAILURE, "stderr" if stream == "stdout" else "stdout": ""}
    )
    success = subprocess.CompletedProcess(
        ["gptme"],
        0,
        stdout=json.dumps({"type": "message", "role": "assistant", "content": answer}),
        stderr="",
    )
    with (
        patch.dict(
            "os.environ",
            {
                "GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1",
                "GPTME_ACTIVITY_SUMMARY_GPTME_MODEL": "openai-subscription/gpt-6.1-sol",
            },
            clear=True,
        ),
        patch("gptme_activity_summary.gptme_backend.shutil.which", return_value="/usr/bin/gptme"),
        patch("gptme_activity_summary.gptme_backend.create_trace_dir", return_value=tmp_path),
        patch("subprocess.run", side_effect=[failure, success]) as run,
        patch("gptme_activity_summary.cc_backend.time.sleep") as sleep,
    ):
        assert call_claude_code("private journal", narrative_key=narrative_key) == answer

    assert run.call_count == 2
    assert run.call_args_list[0].args[0][0] == "claude"
    fallback = run.call_args_list[1]
    assert fallback.args[0][0] == "gptme"
    assert "openai-subscription/gpt-6.1-sol" in fallback.args[0]
    assert fallback.kwargs["input"] == "private journal"
    sleep.assert_not_called()


@pytest.mark.parametrize("opt_in", [None, "0"])
def test_billing_failure_does_not_waive_privacy_opt_in(opt_in):
    """A disabled fallback must raise after one Claude attempt, without a new process."""
    env = {} if opt_in is None else {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": opt_in}
    failure = subprocess.CompletedProcess(["claude"], 1, stdout=BILLING_FAILURE, stderr="")
    with (
        patch.dict("os.environ", env, clear=True),
        patch("subprocess.run", return_value=failure) as run,
        patch("gptme_activity_summary.cc_backend.time.sleep") as sleep,
    ):
        with pytest.raises(ClaudeQuotaExhaustedError) as error:
            call_claude_code("private journal")

    assert error.value.stdout == BILLING_FAILURE
    run.assert_called_once()
    sleep.assert_not_called()


def test_billing_words_in_transient_error_do_not_trigger_fallback():
    """Do not classify arbitrary mentions of billing as a permanent access failure."""
    failure = subprocess.CompletedProcess(
        ["claude"], 1, stdout="", stderr="Temporary connection failure to billing service"
    )
    success = subprocess.CompletedProcess(["claude"], 0, stdout="Recovered", stderr="")
    with (
        patch.dict("os.environ", {"GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK": "1"}, clear=True),
        patch("subprocess.run", side_effect=[failure, success]) as run,
        patch("gptme_activity_summary.cc_backend.time.sleep") as sleep,
    ):
        assert call_claude_code("private journal") == "Recovered"

    assert run.call_count == 2
    assert all(call.args[0][0] == "claude" for call in run.call_args_list)
    sleep.assert_called_once_with(5)
