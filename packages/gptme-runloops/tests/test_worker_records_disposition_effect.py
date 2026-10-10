"""Disposition-only adjudication edits a marker without pushing or replying."""

import json
import subprocess

import pytest
from gptme_runloops.worker_records import (
    apply_pr_state_diff,
    derive_effect_signal,
    fetch_pr_snapshot,
    parse_pr_snapshot,
)

HEAD = "b2c791667f80" + "a" * 28
FP = "54c84dc7958f"


def snapshot(dispositions, *, sha=HEAD[:12], findings=None):
    marker = {
        "sha": sha,
        "findings": findings if findings is not None else [{"fp": FP}],
        "dispositions": dispositions,
    }
    raw = {
        "state": "OPEN",
        "headRefOid": HEAD,
        "comments": [{"body": "<!-- bob-ai-review " + json.dumps(marker) + " -->"}],
    }

    def runner(cmd, **kwargs):
        if cmd[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(cmd, 1, "", "unavailable")
        assert "comments" in cmd[cmd.index("--json") + 1].split(",")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(raw), "")

    return fetch_pr_snapshot("ActivityWatch/aw-webui", 1059, cwd=".", runner=runner)


def test_disposition_only_dispatch_observed_through_snapshot_roundtrip():
    before = snapshot({"old": {"reason": "rejected"}})
    after = snapshot({"old": {"reason": "rejected"}, FP: {"reason": "rejected"}})
    payload = apply_pr_state_diff(
        {}, parse_pr_snapshot(json.dumps(before)), parse_pr_snapshot(json.dumps(after))
    )
    assert payload["pr_ai_review_dispositions_before"] == "0"
    assert payload["pr_ai_review_dispositions_after"] == "1"
    assert payload["deliverables"] == []
    assert derive_effect_signal(payload) == "observed"


@pytest.mark.parametrize("entry", [None, "rejected", 0, []])
def test_non_object_disposition_is_not_effect(entry):
    assert (
        derive_effect_signal(
            apply_pr_state_diff({}, snapshot({}), snapshot({FP: entry}))
        )
        == "none"
    )


def test_stale_review_dispositions_are_not_effect():
    assert (
        derive_effect_signal(
            apply_pr_state_diff(
                {}, snapshot({}, sha="oldhead"), snapshot({FP: {}}, sha="oldhead")
            )
        )
        == "none"
    )


def test_unrelated_disposition_is_not_effect():
    assert (
        derive_effect_signal(
            apply_pr_state_diff({}, snapshot({}), snapshot({"old": {}}))
        )
        == "none"
    )


def test_review_replacement_is_not_effect_even_at_same_head():
    assert (
        derive_effect_signal(
            apply_pr_state_diff(
                {}, snapshot({}), snapshot({"new": {}}, findings=[{"fp": "new"}])
            )
        )
        == "none"
    )


def test_unchanged_and_removed_dispositions_are_not_effect():
    assert (
        derive_effect_signal(
            apply_pr_state_diff({}, snapshot({FP: {}}), snapshot({FP: {}}))
        )
        == "none"
    )
    assert (
        derive_effect_signal(apply_pr_state_diff({}, snapshot({FP: {}}), snapshot({})))
        == "none"
    )


def test_missing_before_marker_is_not_positive_evidence():
    assert (
        derive_effect_signal(
            apply_pr_state_diff({}, {"headRefOid": HEAD}, snapshot({FP: {}}))
        )
        == "none"
    )


def test_head_change_still_counts_without_disposition_signal():
    assert (
        derive_effect_signal(
            {"pr_head_oid_before": HEAD, "pr_head_oid_after": "newhead"}
        )
        == "observed"
    )


@pytest.mark.parametrize(
    "body",
    [
        "<!-- bob-ai-review {broken} -->",
        '<!-- bob-ai-review {"sha":"b2c791667f80","findings":[null]} -->',
        '<!-- bob-ai-review {"sha":"b2c791667f80","findings":[{"fp":null}]} -->',
        '> <!-- bob-ai-review {"sha":"b2c791667f80","findings":[]} -->',
    ],
)
def test_malformed_or_quoted_markers_are_unobserved(body):
    raw = {"headRefOid": HEAD, "comments": [{"body": body}]}
    normalized = parse_pr_snapshot(json.dumps(raw))
    assert "aiReviewDispositions" not in normalized


def test_last_marker_is_used_not_an_older_disposed_review():
    def marker(ds):
        return (
            "<!-- bob-ai-review "
            + json.dumps(
                {"sha": HEAD[:12], "findings": [{"fp": FP}], "dispositions": ds}
            )
            + " -->"
        )

    raw = {
        "headRefOid": HEAD,
        "comments": [{"body": marker({FP: {}})}, {"body": marker({})}],
    }
    assert parse_pr_snapshot(json.dumps(raw))["aiReviewDispositions"] == "0"
