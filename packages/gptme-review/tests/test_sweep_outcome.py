"""Outcome classification for one sweep review attempt."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import mock_open

import pytest
from gptme_review import sweep_outcome as o
from gptme_review.shard import DEFERRED_SENTINEL, EXIT_DEFERRED

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
PR = {"repo": "o/r", "number": 7, "headRefOid": "abc"}


@pytest.mark.parametrize(
    "text",
    [
        "OpenRouter HTTP 429: too many requests",
        "OpenRouter HTTP 402: payment required",
        "Insufficient Balance",
        "API rate limit exceeded for installation",
        "Key limit exceeded (daily limit)",
        # The account-wide cap, as OpenRouter words it (HTTP 403, see #2244 note).
        'OpenRouter HTTP 403: {"error":{"message":"Workspace daily budget of $10.00 '
        'exceeded.","code":403,"metadata":{"limit_source":"openrouter_workspace_budget"}}}',
    ],
)
def test_quota_limited(text: str) -> None:
    assert o.is_quota_limited(text)


def test_workspace_budget_exhaustion_is_not_a_systemic_collapse() -> None:
    """An account-wide daily cap is transient, so the sweep must not exit 1.

    A real run on 2026-11-18 burned 20 review attempts against the workspace
    budget and reported every one as a systemic failure, leaving the unit in
    ``failed`` state for a quota event that resets at midnight.
    """
    text = (
        'ReviewError: OpenRouter HTTP 403: {"error":{"message":"Workspace daily '
        'budget of $10.00 exceeded. Contact your org admin.","code":403,'
        '"metadata":{"limit_source":"openrouter_workspace_budget"}}}'
    )
    assert o.is_quota_limited(text) is True
    assert (
        o.classify_all_failed_run(
            attempted=20,
            failures=20,
            rate_limit_failures=20,
            too_large_failures=0,
        )
        == "rate_limited"
    )


@pytest.mark.parametrize("text", ["", "HTTP 500 internal error", "http 4290 bytes"])
def test_not_quota_limited(text: str) -> None:
    assert not o.is_quota_limited(text)


def test_primary_provider_failure_classes() -> None:
    assert o.is_primary_provider_failure(
        {"engine": "llm", "reason": "OpenRouter did not respond within 300s"}
    )
    assert o.is_primary_provider_failure({"engine": "llm", "rate_limited": True})
    # Oversized diffs and other engines are PR-local, not a transport outage.
    assert not o.is_primary_provider_failure(
        {"engine": "llm", "too_large": True, "rate_limited": True}
    )
    assert not o.is_primary_provider_failure(
        {"engine": "agent", "reason": "openrouter http 500"}
    )
    assert not o.is_primary_provider_failure(
        {"engine": "llm", "reason": "malformed findings JSON"}
    )


@pytest.mark.parametrize(
    "reason",
    [
        "OpenRouter request failed: <urlopen error [Errno 111] Connection refused>",
        "OpenRouter request failed: timed out",
        "OpenRouter request failed: IncompleteRead(128 bytes read, 256 more expected)",
    ],
)
def test_primary_provider_connection_failure(reason: str) -> None:
    assert o.is_primary_provider_failure({"engine": "llm", "reason": reason})
    assert not o.is_primary_provider_failure({"engine": "agent", "reason": reason})
    assert not o.is_primary_provider_failure(
        {"engine": "llm", "reason": reason, "too_large": True}
    )


def test_quota_breaker_needs_zero_successes() -> None:
    n = o.QUOTA_BREAKER_CONSECUTIVE
    assert o.quota_breaker_tripped(n, 0)
    assert not o.quota_breaker_tripped(n - 1, 0)
    assert not o.quota_breaker_tripped(n + 5, 1)


def test_superseded_head_and_provider_extraction() -> None:
    err = "head moved while this review was running; not publishing"
    assert o.is_superseded_head(err)
    assert not o.is_superseded_head("")
    assert o.failed_provider("The provider ('Io Net') returned no answer") == "Io Net"
    assert o.failed_provider("boom") is None


def test_failure_reason_keeps_signal_when_there_is_output() -> None:
    # The reviewer prints progress to stdout; an OOM kill must still say so.
    reason = o.failure_reason(-9, "[shard 1/3] prompt: 12345 chars\n", "")
    assert "SIGKILL" in reason
    assert "[shard 1/3] prompt: 12345 chars" in reason


def test_failure_reason_whitespace_stderr_falls_back_to_stdout() -> None:
    # A bare newline on stderr must not hide the cause printed on stdout,
    # which is_primary_provider_failure reads from this reason.
    assert o.failure_reason(1, "openrouter timed out", "\n") == "openrouter timed out"


def test_failure_reason_keeps_exit_status() -> None:
    assert o.failure_reason(1, "", "line one\nlast line\n") == "last line"
    assert o.failure_reason(1, "stdout only", "") == "stdout only"
    assert o.failure_reason(-9, "", "") == "killed by SIGKILL (exit -9) with no output"
    assert (
        o.failure_reason(-200, None, None)
        == "killed by signal 200 (exit -200) with no output"
    )
    assert o.failure_reason(3, None, None) == "exited 3 with no output"


def test_deferred_review_needs_code_and_sentinel() -> None:
    assert o.is_deferred_review(EXIT_DEFERRED, f"{DEFERRED_SENTINEL} wall clock")
    assert not o.is_deferred_review(EXIT_DEFERRED, "other")
    assert not o.is_deferred_review(1, DEFERRED_SENTINEL)


def test_capacity_shortfall_matches_routing_refusal() -> None:
    assert o.is_subscription_capacity_shortfall(
        "consensus needs 2 but only 1 independent reviewer(s) are available"
    )
    assert not o.is_subscription_capacity_shortfall("independent reviewer found a bug")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"attempted": 0, "failures": 0}, None),
        ({"attempted": 3, "failures": 2}, None),
        ({"attempted": 1, "failures": 1}, "singleton"),
        ({"attempted": 3, "failures": 3, "rate_limit_failures": 3}, "rate_limited"),
        ({"attempted": 3, "failures": 3, "too_large_failures": 3}, "pr_local"),
        (
            {
                "attempted": 3,
                "failures": 3,
                "rate_limit_failures": 1,
                "too_large_failures": 2,
            },
            "pr_local",
        ),
        ({"attempted": 2, "failures": 2, "capacity_failures": 2}, "capacity"),
        ({"attempted": 2, "failures": 2}, "systemic"),
    ],
)
def test_classify_all_failed_run(kwargs: dict, expected: str | None) -> None:
    kwargs.setdefault("rate_limit_failures", 0)
    kwargs.setdefault("too_large_failures", 0)
    assert o.classify_all_failed_run(**kwargs) == expected


def test_failure_backoff_and_quarantine() -> None:
    state: dict[str, object] = {}
    for _ in range(o.DEFAULT_QUARANTINE_STRIKES - 1):
        o.record_failure(state, PR, NOW)
    assert o.recently_failed(PR, state, NOW + timedelta(hours=1))
    assert not o.quarantined_head(PR, state, NOW)

    o.record_failure(state, PR, NOW)
    assert o.failure_count(PR, state) == o.DEFAULT_QUARANTINE_STRIKES
    assert o.quarantined_head(PR, state, NOW)
    # Backoff lapses; hosts may also pass their own window.
    assert not o.quarantined_head(PR, state, NOW + o.DEFAULT_FAILURE_BACKOFF)
    assert not o.quarantined_head(
        PR, state, NOW + timedelta(minutes=5), backoff=timedelta(minutes=1)
    )

    # A new head forgets the old strikes and restarts at one.
    moved = {**PR, "headRefOid": "def"}
    assert o.failure_count(moved, state) == 0
    assert not o.recently_failed(moved, state, NOW)
    o.record_failure(state, moved, NOW)
    assert o.failure_count(moved, state) == 1

    assert o.clear_failure(state, moved)
    assert not o.clear_failure(state, moved)


def test_malformed_failure_rows_fail_open() -> None:
    state: dict[str, object] = {
        "o/r#7": {"head": "abc", "at": "not-a-date", "count": "x"}
    }
    assert not o.recently_failed(PR, state, NOW)
    assert o.failure_count(PR, state) == 0
    assert not o.recently_failed(PR, {"o/r#7": "garbage"}, NOW)


def test_run_process_group_returns_output() -> None:
    done = o.run_process_group([sys.executable, "-c", "print('ok')"], timeout=10)
    assert done.returncode == 0
    assert done.stdout.strip() == "ok"


# None checks the real group kill; injected errors regress the procfs reap race.
@pytest.mark.parametrize(
    "stat_read_error", [None, FileNotFoundError, ProcessLookupError]
)
def test_run_process_group_kills_descendants_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stat_read_error: type[OSError] | None,
) -> None:
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid));"
        "print('started', flush=True);"
        "time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        o.run_process_group([sys.executable, "-c", script], timeout=2)
    assert b"started" in (excinfo.value.output or b"")

    child = int(pid_file.read_text())
    if stat_read_error is not None:
        # A reaped process may disappear after open() but before read().
        stat_file = mock_open()
        stat_file.return_value.read.side_effect = stat_read_error()
        monkeypatch.setattr(sys.modules[__name__], "open", stat_file, raising=False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{child}/stat") as fh:
                if fh.read().split()[2] == "Z":
                    break
        except (FileNotFoundError, ProcessLookupError):
            # procfs can report ENOENT at open() or ESRCH at read() after reap.
            break
        time.sleep(0.05)
    else:
        pytest.fail("grandchild survived the group kill")
    if stat_read_error is not None:
        stat_file.assert_called_once_with(f"/proc/{child}/stat")
        stat_file.return_value.read.assert_called_once_with()
