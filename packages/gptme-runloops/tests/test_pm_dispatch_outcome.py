"""Tests for the derived dispatch outcome invariant.

**Invariant**: a dispatch's recorded outcome is derived from its exit status
and observable effect, never asserted independently of them. ``phase =
"completed"`` only means the item loop returned.

Regression guard for the class where a PM worker exited 1 while its ledger
row said ``completed`` — nothing retried, nothing alerted (gptme/gptme#3468).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gptme_runloops.pm_dispatch import (
    EFFECT_NONE,
    EFFECT_OBSERVED,
    EFFECT_UNKNOWN,
    OUTCOME_FAILED,
    OUTCOME_NO_EFFECT,
    OUTCOME_SUCCEEDED,
    _append_ledger_main,
    append_full_ledger_entry,
    build_full_ledger_entry,
    derive_dispatch_outcome,
)
from gptme_runloops.worker_records import (
    EFFECT_FETCH_FAILED,
    derive_effect_signal,
    read_record_effect_signal,
)


class TestDeriveDispatchOutcome:
    @pytest.mark.parametrize(
        "phase",
        ["launched", "started", "skipped_cap", "skipped_unchanged", "skipped_claimed"],
    )
    def test_non_terminal_phases_carry_no_outcome(self, phase: str) -> None:
        assert derive_dispatch_outcome(phase, 0, 0) is None

    def test_clean_run_succeeds(self) -> None:
        assert derive_dispatch_outcome("completed", 0, 0) == OUTCOME_SUCCEEDED

    def test_nonzero_exit_is_failed_even_with_zero_failures(self) -> None:
        # The exact #3468 shape: worker exited 1, item accounting said fine.
        assert derive_dispatch_outcome("completed", 1, 0) == OUTCOME_FAILED

    def test_failures_are_failed_even_with_zero_exit(self) -> None:
        # The bash executor always exits 0; `failures` is the only signal.
        assert derive_dispatch_outcome("completed", 0, 1) == OUTCOME_FAILED

    def test_both_signals_bad(self) -> None:
        assert derive_dispatch_outcome("completed", 1, 1) == OUTCOME_FAILED

    def test_unknown_exit_code_still_fails_on_failures(self) -> None:
        assert derive_dispatch_outcome("completed", None, 1) == OUTCOME_FAILED
        assert derive_dispatch_outcome("completed", "", 2) == OUTCOME_FAILED

    def test_unknown_exit_code_succeeds_on_zero_failures(self) -> None:
        assert derive_dispatch_outcome("completed", None, 0) == OUTCOME_SUCCEEDED

    def test_no_evidence_at_all_never_asserts_success(self) -> None:
        # The whole point: absence of evidence is not evidence of success.
        assert derive_dispatch_outcome("completed", None, None) is None
        assert derive_dispatch_outcome("completed", "", "") is None

    def test_string_inputs_from_bash_are_coerced(self) -> None:
        assert derive_dispatch_outcome("completed", "0", "0") == OUTCOME_SUCCEEDED
        assert derive_dispatch_outcome("completed", "1", "0") == OUTCOME_FAILED
        assert derive_dispatch_outcome("completed", "0", "3") == OUTCOME_FAILED

    def test_garbage_inputs_do_not_assert_success(self) -> None:
        # Unparseable values coerce to None, not to 0.
        assert derive_dispatch_outcome("completed", "nope", "nope") is None

    def test_negative_exit_code_is_failure(self) -> None:
        # Signal deaths (-9 / -15) are failures, not successes.
        assert derive_dispatch_outcome("completed", -9, 0) == OUTCOME_FAILED


class TestObservableEffectOverridesCleanExit:
    """A clean exit is not evidence the work landed.

    Real instance (2026-08-10, gptme/gptme#3468): the worker read the review
    findings, made the fix, committed it — and every `git push` was rejected
    by a pre-push guard. It exited 0 and its log said it did the work. From
    the outside this was indistinguishable from "PM never ran"; the PR was
    reported as untouched all day.
    """

    def test_no_effect_beats_clean_exit(self) -> None:
        assert (
            derive_dispatch_outcome("completed", 0, 0, EFFECT_NONE) == OUTCOME_NO_EFFECT
        )

    def test_observed_effect_confirms_success(self) -> None:
        assert (
            derive_dispatch_outcome("completed", 0, 0, EFFECT_OBSERVED)
            == OUTCOME_SUCCEEDED
        )

    def test_unknown_effect_does_not_downgrade(self) -> None:
        # Absence of an effect observation is not evidence of no effect. The
        # raw `effect` field is recorded so a reader can still tell the two
        # apart; only an explicit "none" downgrades.
        assert (
            derive_dispatch_outcome("completed", 0, 0, EFFECT_UNKNOWN)
            == OUTCOME_SUCCEEDED
        )
        assert derive_dispatch_outcome("completed", 0, 0, None) == OUTCOME_SUCCEEDED

    def test_failure_outranks_no_effect(self) -> None:
        # An outright failure is the more specific finding.
        assert derive_dispatch_outcome("completed", 1, 0, EFFECT_NONE) == OUTCOME_FAILED

    def test_ledger_records_effect_and_derived_outcome(self) -> None:
        entry = build_full_ledger_entry(
            phase="completed", failures=0, exit_code=0, effect=EFFECT_NONE
        )
        assert entry["effect"] == EFFECT_NONE
        assert entry["outcome"] == OUTCOME_NO_EFFECT

    def test_non_terminal_phase_never_carries_effect(self) -> None:
        entry = build_full_ledger_entry(phase="started", effect=EFFECT_OBSERVED)
        assert entry["effect"] is None
        assert entry["outcome"] is None

    def test_cli_forwards_effect(self, tmp_path: Path) -> None:
        ledger = tmp_path / "dispatch.jsonl"
        _append_ledger_main(
            [
                "--ledger-path",
                str(ledger),
                "--phase",
                "completed",
                "--failures",
                "0",
                "--exit-code",
                "0",
                "--effect",
                EFFECT_NONE,
            ]
        )
        row = json.loads(ledger.read_text().strip())
        assert row["effect"] == EFFECT_NONE
        assert row["outcome"] == OUTCOME_NO_EFFECT


class TestDeriveEffectSignal:
    def test_head_advanced_is_observed(self) -> None:
        assert (
            derive_effect_signal(
                {"pr_head_oid_before": "aaa", "pr_head_oid_after": "bbb"}
            )
            == EFFECT_OBSERVED
        )

    def test_push_rejected_leaves_head_unchanged(self) -> None:
        # THE #3468 shape: commit made locally, push rejected, head identical.
        assert (
            derive_effect_signal(
                {"pr_head_oid_before": "aaa", "pr_head_oid_after": "aaa"}
            )
            == EFFECT_NONE
        )

    def test_oid_comparison_is_case_insensitive(self) -> None:
        assert (
            derive_effect_signal(
                {"pr_head_oid_before": "AAA", "pr_head_oid_after": "aaa"}
            )
            == EFFECT_NONE
        )

    def test_state_transition_is_observed(self) -> None:
        assert (
            derive_effect_signal(
                {"pr_state_before": "OPEN", "pr_state_after": "MERGED"}
            )
            == EFFECT_OBSERVED
        )

    def test_merge_commit_appearing_is_observed(self) -> None:
        assert derive_effect_signal({"pr_merge_commit_after": "ccc"}) == EFFECT_OBSERVED

    def test_missing_snapshots_are_unknown_not_observed(self) -> None:
        # Absence of evidence must never be recorded as evidence of effect.
        assert derive_effect_signal({}) == EFFECT_UNKNOWN
        assert derive_effect_signal({"pr_head_oid_before": "aaa"}) == EFFECT_UNKNOWN

    def test_gh_fetch_failed_flag_returns_fetch_failed(self) -> None:
        """gh_snapshot_fetch_failed flag → EFFECT_FETCH_FAILED, not EFFECT_UNKNOWN."""
        assert (
            derive_effect_signal({"gh_snapshot_fetch_failed": True})
            == EFFECT_FETCH_FAILED
        )

    def test_gh_fetch_failed_overrides_orphan_no_delivery(self) -> None:
        """fetch_failed must outrank orphan_no_delivery — infra failure is not no-effect."""
        assert (
            derive_effect_signal(
                {"gh_snapshot_fetch_failed": True},
                delivery_outcome="orphan_no_delivery",
            )
            == EFFECT_FETCH_FAILED
        )

    def test_orphan_delivery_is_no_effect(self) -> None:
        assert (
            derive_effect_signal(
                {"pr_head_oid_before": "aaa", "pr_head_oid_after": "bbb"},
                delivery_outcome="orphan_no_delivery",
            )
            == EFFECT_NONE
        )

    def test_handled_delivery_is_observed(self) -> None:
        assert derive_effect_signal({}, delivery_outcome="handled") == EFFECT_OBSERVED

    def test_empty_delivery_outcome_is_not_treated_as_handled(self) -> None:
        # run_post_session holds "handled" as its permissive DEFAULT when no
        # delivery check runs, and passes "" instead of that default. If the
        # default ever leaked through, every unchecked session would report
        # observed effect — the exact lie this mechanism exists to prevent.
        assert derive_effect_signal({}, delivery_outcome="") == EFFECT_UNKNOWN
        assert (
            derive_effect_signal(
                {"pr_head_oid_before": "aaa", "pr_head_oid_after": "aaa"},
                delivery_outcome="",
            )
            == EFFECT_NONE
        )

    def test_read_from_record_file(self, tmp_path: Path) -> None:
        record = tmp_path / "record.json"
        record.write_text(
            json.dumps({"pr_head_oid_before": "aaa", "pr_head_oid_after": "aaa"})
        )
        assert read_record_effect_signal(record) == EFFECT_NONE

    def test_read_missing_or_bad_record_is_unknown(self, tmp_path: Path) -> None:
        assert read_record_effect_signal(tmp_path / "nope.json") == EFFECT_UNKNOWN
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        assert read_record_effect_signal(bad) == EFFECT_UNKNOWN
        arr = tmp_path / "arr.json"
        arr.write_text("[1,2]")
        assert read_record_effect_signal(arr) == EFFECT_UNKNOWN


class TestLedgerEntryCarriesDerivedOutcome:
    def test_completed_with_failures_records_failed(self) -> None:
        entry = build_full_ledger_entry(
            phase="completed", successes=0, failures=1, exit_code=1
        )
        assert entry["phase"] == "completed"
        assert entry["exit_code"] == 1
        assert entry["outcome"] == OUTCOME_FAILED

    def test_completed_clean_records_succeeded(self) -> None:
        entry = build_full_ledger_entry(
            phase="completed", successes=2, failures=0, exit_code=0
        )
        assert entry["outcome"] == OUTCOME_SUCCEEDED

    def test_bash_path_without_exit_code_still_derives(self) -> None:
        # project-monitoring.sh passes exit_code="" on purpose (it has no
        # aggregate worker exit status and must not fabricate a 0).
        entry = build_full_ledger_entry(
            phase="completed", successes=0, failures=1, exit_code=""
        )
        assert entry["exit_code"] is None
        assert entry["outcome"] == OUTCOME_FAILED

    def test_skip_phases_have_null_outcome(self) -> None:
        entry = build_full_ledger_entry(phase="skipped_cap", note="cap")
        assert entry["outcome"] is None
        assert entry["exit_code"] is None

    def test_schema_keys_are_additive_only(self) -> None:
        entry = build_full_ledger_entry(phase="started")
        for legacy_key in (
            "timestamp",
            "phase",
            "lane",
            "dispatch_id",
            "unit",
            "item_count",
            "item_refs",
            "types",
            "items",
            "running_units",
            "cap",
            "note",
            "successes",
            "failures",
            "duration_seconds",
        ):
            assert legacy_key in entry
        assert {"exit_code", "outcome"} <= set(entry)


class TestAppendLedgerCli:
    def test_cli_forwards_exit_code(self, tmp_path: Path) -> None:
        ledger = tmp_path / "dispatch.jsonl"
        rc = _append_ledger_main(
            [
                "--ledger-path",
                str(ledger),
                "--phase",
                "completed",
                "--successes",
                "0",
                "--failures",
                "1",
                "--exit-code",
                "1",
            ]
        )
        assert rc == 0
        row = json.loads(ledger.read_text().strip())
        assert row["exit_code"] == 1
        assert row["outcome"] == OUTCOME_FAILED

    def test_cli_without_exit_code_derives_from_failures(self, tmp_path: Path) -> None:
        ledger = tmp_path / "dispatch.jsonl"
        _append_ledger_main(
            [
                "--ledger-path",
                str(ledger),
                "--phase",
                "completed",
                "--successes",
                "0",
                "--failures",
                "2",
            ]
        )
        row = json.loads(ledger.read_text().strip())
        assert row["exit_code"] is None
        assert row["outcome"] == OUTCOME_FAILED

    def test_appended_entry_round_trips(self, tmp_path: Path) -> None:
        ledger = tmp_path / "dispatch.jsonl"
        written = append_full_ledger_entry(
            ledger, phase="completed", failures=0, exit_code=0
        )
        assert written["outcome"] == OUTCOME_SUCCEEDED
        assert json.loads(ledger.read_text().strip()) == written
