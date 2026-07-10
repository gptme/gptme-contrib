"""Golden tests for the merge-lifecycle state machine.

Table-driven encodings of the CURRENT bash behavior of
``run_self_merge_and_greptile`` (ErikBjare/bob
``scripts/github/project-monitoring-lib.sh:519-609``) and the head-gated
convergence counter (``pr-merge-health-poll.py`` ``apply_convergence_cap``
per-signal core, commit f1fce829fb). Where the bash behavior is quirky the
test name/comment says so — these tests lock in parity, they do not bless
the quirk. Behavior changes belong to the brain-side switchover step.

Invariants from project-monitoring-architecture.md §7b covered here:
- self-merge only via the gate (SELF_MERGE requires eligible=True)
- Greptile triggering only via greptile-helper (helper missing → no trigger)
- an attempt = pushed commits (head-gated convergence counting)
- human > bot (as-implemented: human-thread reasons route to a fix session)
"""

from __future__ import annotations

from typing import Any

import pytest
from gptme_runloops.merge_lifecycle import (
    AttemptDecision,
    InstructionKind,
    LifecycleConfig,
    LifecycleDecision,
    MergeLifecycleAction,
    SelfMergeBlockClass,
    SelfMergeCheckResult,
    WorkItem,
    classify_greptile_helper_status,
    classify_self_merge_reasons,
    cross_repo_review_applicable,
    decide_cross_repo_review,
    decide_greptile_attempt,
    decide_self_merge_gate,
    head_advanced,
    parse_greptile_score,
    run_merge_lifecycle,
    self_merge_gate_applicable,
)

# The brain's policy shape (lib.sh:577,579) — passed as config, not baked in.
# (The real brain pattern lists one more repo; the exact repo set is caller
# policy, not module behavior.)
BOB_CONFIG = LifecycleConfig(
    primary_repo="ErikBjare/bob",
    greptile_repos_pattern=r"^(gptme/gptme|gptme/gptme-contrib|gptme/gptme-cloud)$",
)


def make_item(
    repo: str = "gptme/gptme",
    number: int = 123,
    types: tuple[str, ...] = ("pr_update",),
) -> WorkItem:
    return WorkItem(repo=repo, number=number, types=types)


# --- FakeIO: records the call sequence, returns scripted responses ---


class FakeIO:
    def __init__(
        self,
        check: SelfMergeCheckResult | None = None,
        merge_ok: bool = False,
        status: str = "already-reviewed",
    ) -> None:
        self._check = check or SelfMergeCheckResult(eligible=False)
        self._merge_ok = merge_ok
        self._status = status
        self.calls: list[tuple[str, str, Any]] = []

    def self_merge_check(self, repo: str, number: Any) -> SelfMergeCheckResult:
        self.calls.append(("self_merge_check", repo, number))
        return self._check

    def self_merge(self, repo: str, number: Any) -> bool:
        self.calls.append(("self_merge", repo, number))
        return self._merge_ok

    def greptile_status(self, repo: str, number: Any) -> str:
        self.calls.append(("greptile_status", repo, number))
        return self._status

    def trigger_review(self, repo: str, number: Any) -> None:
        self.calls.append(("trigger_review", repo, number))

    def promote_item_state(self, repo: str, number: Any) -> None:
        self.calls.append(("promote_item_state", repo, number))

    def called(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)


# --- Reason classification (lib.sh:551-572 grep routing) ---


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        # Empty / unrelated reasons fall through to OTHER.
        ([], SelfMergeBlockClass.OTHER),
        (["PR is still a draft"], SelfMergeBlockClass.OTHER),
        (["CI is not fully green"], SelfMergeBlockClass.OTHER),
        (["Review decision: CHANGES_REQUESTED"], SelfMergeBlockClass.OTHER),
        # Exact upstream reason strings (self-merge-check.py evaluate_pr).
        (["Greptile review not found"], SelfMergeBlockClass.NO_GREPTILE_REVIEW),
        (
            ["Greptile has 2 unresolved review thread(s)"],
            SelfMergeBlockClass.UNRESOLVED_THREADS,
        ),
        (
            ["Greptile score 3/5 below floor 5/5"],
            SelfMergeBlockClass.SCORE_BELOW_FLOOR,
        ),
        # Priority: not-found wins even when other blockers are present —
        # the bash greps the FULL joined reasons text in a fixed order.
        (
            ["CI is not fully green", "Greptile review not found"],
            SelfMergeBlockClass.NO_GREPTILE_REVIEW,
        ),
        (
            ["Greptile review not found", "Greptile score 3/5 below floor 5/5"],
            SelfMergeBlockClass.NO_GREPTILE_REVIEW,
        ),
        # Priority: unresolved threads beat the score floor.
        (
            [
                "Greptile has 1 unresolved review thread(s)",
                "Greptile score 3/5 below floor 5/5",
            ],
            SelfMergeBlockClass.UNRESOLVED_THREADS,
        ),
        # NOTE(parity): the HUMAN-thread reason does NOT match the
        # "unresolved review thread" grep — the word "human" breaks the
        # substring — so human-only blocks route to OTHER (proceed with a
        # session, no Greptile fix instructions). Human>bot is enforced by
        # the gate (merge stays blocked) + the worker prompt, not here.
        (
            ["2 unresolved human review thread(s) from: ErikBjare"],
            SelfMergeBlockClass.OTHER,
        ),
    ],
)
def test_classify_self_merge_reasons(
    reasons: list[str], expected: SelfMergeBlockClass
) -> None:
    assert classify_self_merge_reasons(reasons) is expected


# --- Gate JSON parsing (lib.sh:540,550 via _json_get) ---


@pytest.mark.parametrize(
    ("raw", "eligible", "reasons"),
    [
        ('{"eligible": true, "reasons": []}', True, ()),
        (
            '{"eligible": false, "reasons": ["CI is not fully green"]}',
            False,
            ("CI is not fully green",),
        ),
        # Parse failures fail closed (bash: `|| sm_eligible="False"`).
        ("", False, ()),
        ("not json", False, ()),
        ("[1, 2]", False, ()),
        ("{}", False, ()),
        # NOTE(parity): bash compares str() of the JSON value to "True", so
        # the JSON *string* "True" passes but "true" does not.
        ('{"eligible": "True"}', True, ()),
        ('{"eligible": "true"}', False, ()),
        ('{"eligible": 1}', False, ()),
    ],
)
def test_self_merge_check_result_from_json(
    raw: str, eligible: bool, reasons: tuple[str, ...]
) -> None:
    result = SelfMergeCheckResult.from_json(raw)
    assert result.eligible is eligible
    assert result.reasons == reasons


# --- Phase A decision (lib.sh:534-575) ---


def test_phase_a_gating_by_types() -> None:
    assert self_merge_gate_applicable(make_item(types=("pr_update",)))
    assert self_merge_gate_applicable(make_item(types=("merge_ready",)))
    assert self_merge_gate_applicable(make_item(types=("pr_update", "ci_failure")))
    assert not self_merge_gate_applicable(make_item(types=("assigned_issue",)))
    assert not self_merge_gate_applicable(make_item(types=()))
    # Gate script missing → whole phase skipped.
    assert not self_merge_gate_applicable(
        make_item(types=("pr_update",)), gate_available=False
    )


def test_phase_a_not_applicable_decision() -> None:
    decision = decide_self_merge_gate(make_item(types=("assigned_issue",)), None)
    assert decision.action is MergeLifecycleAction.NOT_APPLICABLE


def test_phase_a_applicable_requires_check_result() -> None:
    with pytest.raises(ValueError):
        decide_self_merge_gate(make_item(), None)


@pytest.mark.parametrize(
    ("check", "action", "instructions"),
    [
        # §7b: self-merge only via the gate — SELF_MERGE iff eligible.
        (SelfMergeCheckResult(eligible=True), MergeLifecycleAction.SELF_MERGE, None),
        (
            SelfMergeCheckResult(False, ("Greptile review not found",)),
            MergeLifecycleAction.TRIGGER_REVIEW,
            None,
        ),
        (
            SelfMergeCheckResult(
                False, ("Greptile has 1 unresolved review thread(s)",)
            ),
            MergeLifecycleAction.FIX_FINDINGS,
            InstructionKind.LOCAL_GREPTILE_FIX,
        ),
        (
            SelfMergeCheckResult(False, ("Greptile score 4/5 below floor 5/5",)),
            MergeLifecycleAction.FIX_FINDINGS,
            InstructionKind.LOCAL_GREPTILE_FIX,
        ),
        (
            SelfMergeCheckResult(False, ("PR is still a draft",)),
            MergeLifecycleAction.PROCEED,
            None,
        ),
        # Failed/empty gate output → not eligible with no reasons → PROCEED.
        (SelfMergeCheckResult(False, ()), MergeLifecycleAction.PROCEED, None),
    ],
)
def test_phase_a_decision_table(
    check: SelfMergeCheckResult,
    action: MergeLifecycleAction,
    instructions: InstructionKind | None,
) -> None:
    decision = decide_self_merge_gate(make_item(), check)
    assert decision.action is action
    assert decision.instructions is instructions


def test_phase_a_proceed_reason_carries_first_reason_line() -> None:
    check = SelfMergeCheckResult(
        False, ("PR is still a draft", "CI is not fully green")
    )
    decision = decide_self_merge_gate(make_item(), check)
    # lib.sh:572 logs `head -1` of the reasons.
    assert "PR is still a draft" in decision.reason
    assert "CI is not fully green" not in decision.reason


# --- Phase B gating (lib.sh:579) ---


@pytest.mark.parametrize(
    ("repo", "types", "pending", "expected"),
    [
        ("gptme/gptme", ("pr_update",), False, True),
        ("gptme/gptme-contrib", ("pr_update",), False, True),
        ("gptme/gptme-cloud", ("pr_update",), False, True),
        # Fix instructions already queued → phase skipped.
        ("gptme/gptme", ("pr_update",), True, False),
        # Phase B needs pr_update specifically; merge_ready alone is not enough.
        ("gptme/gptme", ("merge_ready",), False, False),
        # Primary repo excluded (redundant with the pattern, but a separate
        # condition in the bash — preserved).
        ("ErikBjare/bob", ("pr_update",), False, False),
        # NOTE(parity): gptme/gptme-webui is PM-tracked but absent from the
        # cross-repo Greptile pattern, so its PRs never get the lifecycle.
        ("gptme/gptme-webui", ("pr_update",), False, False),
        ("ActivityWatch/activitywatch", ("pr_update",), False, False),
        # Anchored pattern: no substring matches.
        ("gptme/gptme-extra", ("pr_update",), False, False),
    ],
)
def test_phase_b_applicability(
    repo: str, types: tuple[str, ...], pending: bool, expected: bool
) -> None:
    item = make_item(repo=repo, types=types)
    assert (
        cross_repo_review_applicable(item, BOB_CONFIG, fix_instructions_pending=pending)
        is expected
    )


# --- Phase B status routing (lib.sh:586-604) ---


@pytest.mark.parametrize(
    ("status", "action", "instructions"),
    [
        # NOTE(parity): "none" is a dead arm — the helper's status command
        # never emits it — but the bash case preserves it, so we do too.
        ("none", MergeLifecycleAction.TRIGGER_REVIEW, None),
        ("stale", MergeLifecycleAction.TRIGGER_REVIEW, None),
        (
            "needs-re-review",
            MergeLifecycleAction.FIX_FINDINGS,
            InstructionKind.CROSS_REPO_GREPTILE_REFRESH,
        ),
        ("in-progress", MergeLifecycleAction.WAIT, None),
        ("already-reviewed", MergeLifecycleAction.PROCEED, None),
        # Unknown states (incl. real helper outputs the bash has no arm for)
        # fall through to skip.
        ("awaiting-initial-review", MergeLifecycleAction.WAIT, None),
        ("backoff", MergeLifecycleAction.WAIT, None),
        ("error", MergeLifecycleAction.WAIT, None),
        ("garbage", MergeLifecycleAction.WAIT, None),
    ],
)
def test_phase_b_status_table(
    status: str, action: MergeLifecycleAction, instructions: InstructionKind | None
) -> None:
    decision = classify_greptile_helper_status(status)
    assert decision.action is action
    assert decision.instructions is instructions


def test_decide_cross_repo_review_helper_missing_proceeds_without_trigger() -> None:
    # lib.sh:580-581 — WARN + skip; §7b: no trigger path outside the helper.
    decision = decide_cross_repo_review(
        make_item(), BOB_CONFIG, None, helper_available=False
    )
    assert decision.action is MergeLifecycleAction.PROCEED


def test_decide_cross_repo_review_applicable_requires_status() -> None:
    with pytest.raises(ValueError):
        decide_cross_repo_review(make_item(), BOB_CONFIG, None)


# --- Orchestrator: call-sequence golden tests ---


def test_self_merge_success_skips_session_and_promotes_state() -> None:
    io = FakeIO(check=SelfMergeCheckResult(eligible=True), merge_ok=True)
    result = run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    assert result.skip_item is True
    assert result.instructions is None
    assert [c[0] for c in io.calls] == [
        "self_merge_check",
        "self_merge",
        "promote_item_state",
    ]
    # Phase B never runs after a successful self-merge (bash `return 1`).
    assert io.called("greptile_status") == 0


def test_self_merge_failure_falls_through_silently_to_phase_b() -> None:
    # NOTE(parity): eligible-but-merge-failed produces no message, no fix
    # instructions, no retry — the bash falls through to the cross-repo check.
    io = FakeIO(
        check=SelfMergeCheckResult(eligible=True),
        merge_ok=False,
        status="already-reviewed",
    )
    result = run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    assert result.skip_item is False
    assert result.instructions is None
    assert io.called("promote_item_state") == 0
    assert io.called("greptile_status") == 1


def test_self_merge_never_attempted_when_not_eligible() -> None:
    # §7b: self-merge only via the gate.
    io = FakeIO(check=SelfMergeCheckResult(False, ("CI is not fully green",)))
    run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    assert io.called("self_merge") == 0


def test_no_review_triggers_via_helper_then_evaluates_phase_b() -> None:
    io = FakeIO(
        check=SelfMergeCheckResult(False, ("Greptile review not found",)),
        status="in-progress",
    )
    result = run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    # Phase A triggered once; no instructions queued, so Phase B still runs
    # (bash: GREPTILE_FIX_INSTRUCTIONS stays empty after a trigger).
    assert io.called("trigger_review") == 1
    assert io.called("greptile_status") == 1
    assert result.instructions is None


def test_no_review_can_double_trigger_when_phase_b_sees_stale() -> None:
    # NOTE(parity): current bash can invoke the helper trigger in BOTH
    # phases in one pass (Phase A on "review not found", Phase B on
    # status=stale). The helper's own flock/age/ceiling guards are the
    # dedupe layer, so this is trigger-request duplication, not spam.
    io = FakeIO(
        check=SelfMergeCheckResult(False, ("Greptile review not found",)),
        status="stale",
    )
    run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    assert io.called("trigger_review") == 2


def test_no_review_with_helper_missing_never_triggers() -> None:
    # §7b: greptile-helper-only triggering — no helper, no trigger, ever.
    io = FakeIO(check=SelfMergeCheckResult(False, ("Greptile review not found",)))
    run_merge_lifecycle(make_item(), BOB_CONFIG, io, helper_available=False)
    assert io.called("trigger_review") == 0
    assert io.called("greptile_status") == 0


def test_unresolved_threads_queue_local_fix_and_skip_phase_b() -> None:
    io = FakeIO(
        check=SelfMergeCheckResult(
            False, ("Greptile has 3 unresolved review thread(s)",)
        )
    )
    result = run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    assert result.instructions is InstructionKind.LOCAL_GREPTILE_FIX
    # Fix instructions queued in Phase A gate Phase B off (lib.sh:579 -z check).
    assert io.called("greptile_status") == 0


def test_score_below_floor_queues_local_fix() -> None:
    io = FakeIO(
        check=SelfMergeCheckResult(False, ("Greptile score 2/5 below floor 5/5",))
    )
    result = run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    assert result.instructions is InstructionKind.LOCAL_GREPTILE_FIX


def test_other_reason_proceeds_then_runs_phase_b() -> None:
    io = FakeIO(
        check=SelfMergeCheckResult(False, ("PR is still a draft",)),
        status="needs-re-review",
    )
    result = run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    assert result.instructions is InstructionKind.CROSS_REPO_GREPTILE_REFRESH
    assert io.called("greptile_status") == 1


def test_primary_repo_item_never_runs_phase_b() -> None:
    io = FakeIO(check=SelfMergeCheckResult(False, ("CI is not fully green",)))
    run_merge_lifecycle(make_item(repo="ErikBjare/bob"), BOB_CONFIG, io)
    assert io.called("greptile_status") == 0


def test_merge_ready_only_item_runs_phase_a_but_not_phase_b() -> None:
    io = FakeIO(check=SelfMergeCheckResult(False, ("CI is not fully green",)))
    run_merge_lifecycle(make_item(types=("merge_ready",)), BOB_CONFIG, io)
    assert io.called("self_merge_check") == 1
    assert io.called("greptile_status") == 0


def test_gate_unavailable_skips_phase_a_but_not_phase_b() -> None:
    io = FakeIO(status="stale")
    run_merge_lifecycle(make_item(), BOB_CONFIG, io, gate_available=False)
    assert io.called("self_merge_check") == 0
    assert io.called("greptile_status") == 1
    assert io.called("trigger_review") == 1


def test_phase_b_in_progress_waits_without_trigger() -> None:
    io = FakeIO(
        check=SelfMergeCheckResult(False, ("CI is not fully green",)),
        status="in-progress",
    )
    result = run_merge_lifecycle(make_item(), BOB_CONFIG, io)
    assert io.called("trigger_review") == 0
    assert result.instructions is None
    assert any(d.action is MergeLifecycleAction.WAIT for d in result.decisions)


def test_fix_instructions_pending_gates_phase_b_off() -> None:
    io = FakeIO(check=SelfMergeCheckResult(False, ("CI is not fully green",)))
    run_merge_lifecycle(make_item(), BOB_CONFIG, io, fix_instructions_pending=True)
    assert io.called("greptile_status") == 0


def test_helper_missing_phase_b_warns_and_proceeds() -> None:
    io = FakeIO(check=SelfMergeCheckResult(False, ("CI is not fully green",)))
    result = run_merge_lifecycle(make_item(), BOB_CONFIG, io, helper_available=False)
    assert io.called("greptile_status") == 0
    assert result.decisions[-1].action is MergeLifecycleAction.PROCEED


def test_non_pr_item_is_a_full_noop() -> None:
    io = FakeIO()
    result = run_merge_lifecycle(make_item(types=("assigned_issue",)), BOB_CONFIG, io)
    assert io.calls == []
    assert result.skip_item is False
    assert result.instructions is None


# --- Head-gated convergence counter (f1fce829fb; poll.py:430-502) ---

NOW = "2026-07-10T12:00:00+00:00"


def test_attempt_first_observation_counts() -> None:
    decision, entry = decide_greptile_attempt({}, "abc1234", 3, NOW, score=3)
    assert decision is AttemptDecision.DISPATCH
    assert entry["count"] == 1
    assert entry["last_attempt_head"] == "abc1234"
    assert entry["first_attempt_at"] == NOW


def test_attempt_same_head_reemit_is_free() -> None:
    # §7b invariant: an attempt = pushed commits. A re-emit at the same head
    # (slot-starved dispatch or no-op session) dispatches again but does not
    # burn budget.
    _, entry = decide_greptile_attempt({}, "abc1234", 3, NOW, score=3)
    decision, entry2 = decide_greptile_attempt(entry, "abc1234", 3, NOW, score=3)
    assert decision is AttemptDecision.DISPATCH_FREE
    assert entry2["count"] == 1


def test_attempt_head_advance_consumes_budget() -> None:
    _, entry = decide_greptile_attempt({}, "abc1234", 3, NOW, score=3)
    decision, entry2 = decide_greptile_attempt(entry, "def5678", 3, NOW, score=3)
    assert decision is AttemptDecision.DISPATCH
    assert entry2["count"] == 2
    assert entry2["last_attempt_head"] == "def5678"


def test_attempt_many_same_head_reemits_never_escalate() -> None:
    # Incident 2026-07-09 (gptme-contrib#1253): pre-fix, poll re-emits alone
    # escalated a never-dispatched PR. Post-fix, unlimited same-head re-emits
    # stay below the cap.
    _, entry = decide_greptile_attempt({}, "abc1234", 3, NOW, score=3)
    for _ in range(10):
        decision, entry = decide_greptile_attempt(entry, "abc1234", 3, NOW, score=3)
    assert decision is AttemptDecision.DISPATCH_FREE
    assert entry["count"] == 1
    assert not entry["escalated"]


def test_attempt_cap_escalates() -> None:
    entry: dict[str, Any] = {}
    decision = AttemptDecision.DISPATCH
    heads = ["h1", "h2", "h3", "h4"]
    for head in heads:
        decision, entry = decide_greptile_attempt(entry, head, 3, NOW, score=3)
    assert decision is AttemptDecision.ESCALATE
    assert entry["escalated"] is True
    assert entry["escalated_at"] == NOW


def test_attempt_escalated_with_head_history_stays_escalated() -> None:
    entry = {"count": 3, "escalated": True, "last_attempt_head": "h3"}
    decision, updated = decide_greptile_attempt(entry, "h4", 3, NOW, score=3)
    assert decision is AttemptDecision.ESCALATE
    assert updated["escalated"] is True


def test_attempt_legacy_escalation_without_head_history_resets() -> None:
    # Legacy escalations (pre-head-gating) carry no last_attempt_head; they
    # get a fresh start under the head-gated counter (poll.py:432-443).
    entry = {"count": 3, "escalated": True}
    decision, updated = decide_greptile_attempt(entry, "h1", 3, NOW, score=3)
    assert decision is AttemptDecision.DISPATCH
    assert updated["escalated"] is False
    assert updated["count"] == 1
    assert updated["last_attempt_head"] == "h1"


def test_attempt_post_rebase_is_free_and_consumes_grace() -> None:
    _, entry = decide_greptile_attempt({}, "h1", 3, NOW, score=3)
    decision, updated = decide_greptile_attempt(
        entry, "h2", 3, NOW, score=3, post_rebase=True
    )
    assert decision is AttemptDecision.DISPATCH_FREE
    assert updated["count"] == 1
    assert updated["conflict_grace_used_at"] == NOW
    assert updated["last_attempt_head"] == "h2"


def test_attempt_post_rebase_at_cap_still_escalates() -> None:
    # poll.py checks the cap BEFORE the post-rebase branch.
    entry = {"count": 3, "last_attempt_head": "h3"}
    decision, updated = decide_greptile_attempt(
        entry, "h4", 3, NOW, score=3, post_rebase=True
    )
    assert decision is AttemptDecision.ESCALATE
    assert updated["escalated"] is True


def test_attempt_empty_head_never_counts_after_first_observation() -> None:
    # NOTE(parity): an unparseable dedupe key yields cur_head="" which the
    # bool() guard keeps from consuming budget or clobbering the stored head.
    _, entry = decide_greptile_attempt({}, "h1", 3, NOW, score=3)
    decision, updated = decide_greptile_attempt(entry, "", 3, NOW, score=3)
    assert decision is AttemptDecision.DISPATCH_FREE
    assert updated["count"] == 1
    assert updated["last_attempt_head"] == "h1"


@pytest.mark.parametrize(
    ("cur_head", "last_head", "expected"),
    [
        ("h1", None, True),  # first observation counts
        ("h1", "h1", False),  # same head is free
        ("h2", "h1", True),  # head advanced
        ("", "h1", False),  # unparseable head never counts
        ("", None, True),  # first observation counts even with no head
    ],
)
def test_head_advanced_table(
    cur_head: str, last_head: str | None, expected: bool
) -> None:
    assert head_advanced(cur_head, last_head) is expected


# --- Greptile score parsing (greptile-helper.sh:186) ---


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Confidence Score: 4/5", 4),
        ("**Confidence Score:** 3/5", 3),
        ("Score: 5/5", 5),
        ("no score here", None),
        ("", None),
        # First match wins (jq capture semantics).
        ("Score: 2/5 ... later Score: 5/5", 2),
        # Only single digits over /5 parse (regex is [0-9] right after the
        # Score prefix, so "10/5" matches nothing — same in jq).
        ("Score: 10/5", None),
    ],
)
def test_parse_greptile_score(body: str, expected: int | None) -> None:
    assert parse_greptile_score(body) == expected


# --- Decision dataclass sanity ---


def test_lifecycle_decision_is_frozen() -> None:
    decision = LifecycleDecision(MergeLifecycleAction.WAIT, "x")
    with pytest.raises(AttributeError):
        decision.reason = "y"  # type: ignore[misc]
