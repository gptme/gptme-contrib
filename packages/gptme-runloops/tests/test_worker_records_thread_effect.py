"""Review-thread resolution is an observable effect.

Regression guard for gptme/gptme#3613: PM re-armed the item twice as
``retry_budget_exhausted:ineffective`` while its only outstanding work was a
single already-fixed review thread. Resolving that thread leaves no commit and
needs no reply, so the pre-fix ``derive_effect_signal`` could only read it as
``none``.
"""

import subprocess

from gptme_runloops.worker_records import (
    apply_pr_state_diff,
    derive_effect_signal,
    fetch_pr_snapshot,
    fetch_unresolved_thread_count,
)

HEAD = "a" * 40


def _payload(before: str | None, after: str | None) -> dict:
    payload: dict = {"pr_head_oid_before": HEAD, "pr_head_oid_after": HEAD}
    if before is not None:
        payload["pr_unresolved_threads_before"] = before
    if after is not None:
        payload["pr_unresolved_threads_after"] = after
    return payload


def test_resolving_a_thread_is_an_observed_effect():
    assert derive_effect_signal(_payload("1", "0")) == "observed"


def test_unchanged_threads_with_unchanged_head_is_none():
    assert derive_effect_signal(_payload("1", "1")) == "none"


def test_zero_before_and_after_is_none_not_observed():
    """`0` is a real observation, not a missing one — it must not read as effect."""
    assert derive_effect_signal(_payload("0", "0")) == "none"


def test_new_thread_filed_mid_dispatch_is_not_an_effect():
    """A reviewer filing a thread is not something this session produced."""
    assert derive_effect_signal(_payload("0", "2")) == "none"


def test_thread_counts_alone_can_settle_none_without_head_signal():
    assert (
        derive_effect_signal(
            {"pr_unresolved_threads_before": "3", "pr_unresolved_threads_after": "3"}
        )
        == "none"
    )


def test_unobserved_counts_degrade_to_prior_behaviour():
    """Absent counts must leave the verdict exactly as it was pre-fix."""
    assert derive_effect_signal({}) == "unknown"
    assert derive_effect_signal(_payload(None, "0")) == "none"  # head pair settles it
    assert derive_effect_signal({"pr_unresolved_threads_after": "0"}) == "unknown"


def test_garbage_thread_count_is_treated_as_unobserved():
    assert (
        derive_effect_signal(
            {"pr_unresolved_threads_before": "many", "pr_unresolved_threads_after": "0"}
        )
        == "unknown"
    )


def test_apply_pr_state_diff_records_both_sides():
    payload = apply_pr_state_diff(
        {},
        {"headRefOid": HEAD, "unresolvedThreads": "2"},
        {"headRefOid": HEAD, "unresolvedThreads": "0"},
    )
    assert payload["pr_unresolved_threads_before"] == "2"
    assert payload["pr_unresolved_threads_after"] == "0"
    assert derive_effect_signal(payload) == "observed"


def _runner(graphql_stdout: str, graphql_rc: int = 0):
    def run(cmd, **kwargs):
        if cmd[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(cmd, graphql_rc, graphql_stdout, "")
        return subprocess.CompletedProcess(
            cmd, 0, '{"state":"OPEN","headRefOid":"' + HEAD + '"}', ""
        )

    return run


def test_fetch_counts_only_unresolved_threads():
    body = (
        '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":'
        '[{"isResolved":true},{"isResolved":false},{"isResolved":false}]}}}}}'
    )
    assert (
        fetch_unresolved_thread_count(
            "gptme/gptme", 3613, cwd=".", runner=_runner(body)
        )
        == 2
    )


def test_fetch_returns_none_on_failure_and_snapshot_still_works():
    assert (
        fetch_unresolved_thread_count(
            "gptme/gptme", 3613, cwd=".", runner=_runner("", graphql_rc=1)
        )
        is None
    )
    snapshot = fetch_pr_snapshot(
        "gptme/gptme", 3613, cwd=".", runner=_runner("", graphql_rc=1)
    )
    assert snapshot is not None
    assert "unresolvedThreads" not in snapshot


def test_fetch_rejects_malformed_repo():
    assert (
        fetch_unresolved_thread_count("notarepo", 1, cwd=".", runner=_runner("{}"))
        is None
    )


def test_snapshot_carries_thread_count_when_available():
    body = (
        '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":'
        '[{"isResolved":false}]}}}}}'
    )
    snapshot = fetch_pr_snapshot("gptme/gptme", 3613, cwd=".", runner=_runner(body))
    assert snapshot is not None
    assert snapshot["unresolvedThreads"] == "1"
