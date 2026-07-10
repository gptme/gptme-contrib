"""PR merge-lifecycle state machine (pure decision core + thin gh I/O).

Behavior-identical port of the bash ``run_self_merge_and_greptile`` state
machine from ErikBjare/bob ``scripts/github/project-monitoring-lib.sh``
(step 1 of the Phase-2 execution-consolidation migration; see
``knowledge/technical-designs/reactive-dispatch-phase2-3-execution-consolidation.md``
in that repo). The bash remains the runtime hotpath until the brain-side
switchover PR; this module is the reviewable, golden-tested reference the
switchover will diff against.

Bash source lines ported (ErikBjare/bob @ dcb4bd837cf1d214f6f37e4d5c67e4029382fa61):

- ``project-monitoring-lib.sh:519-609`` — ``run_self_merge_and_greptile``:
  the two-phase state machine (self-merge gate + reason routing, then the
  cross-repo Greptile status check). Ported as :func:`decide_self_merge_gate`,
  :func:`decide_cross_repo_review`, and the :func:`run_merge_lifecycle`
  orchestrator.
- ``project-monitoring-lib.sh:577`` — the cross-repo Greptile repo pattern
  (accepted as config; Bob-side policy values stay in the brain).
- ``pr-merge-health-poll.py:406-504`` (ErikBjare/bob @
  6eae7cc00a95dabaf440e589e80a1caaf378cc80) — the head-gated per-PR
  convergence attempt counter (``f1fce829fb``: an attempt = pushed commits).
  Ported as :func:`decide_greptile_attempt`. The sweep-level pieces
  (counter reset for recovered PRs, conflict-timestamp recording, the
  post-rebase *time-window* detection) stay in the poller; ``post_rebase``
  is accepted as a computed input here.
- ``greptile-helper.sh:186`` — the Greptile confidence-score regex, ported
  as :func:`parse_greptile_score`.

Self-merge eligibility *reason* strings matched by the reason router are
produced by ``scripts/github/self-merge-check.py`` (this repo,
``evaluate_pr``): ``"Greptile review not found"``, ``"Greptile has N
unresolved review thread(s)"``, ``"Greptile score N/5 below floor M/5"``.

Invariants encoded (project-monitoring-architecture.md §7b):

- **Self-merge only via the gate**: the only path to ``SELF_MERGE`` is an
  ``eligible=True`` gate result; the I/O adapter merges only through the
  self-merge script.
- **Greptile-helper-only triggering**: ``TRIGGER_REVIEW`` is executed solely
  through the helper (which owns the anti-spam guards); when the helper is
  unavailable the trigger is skipped, never inlined.
- **An attempt = pushed commits**: :func:`decide_greptile_attempt` consumes
  convergence budget only when the PR head advanced since the last attempt.
- **Human > bot** is preserved *as the bash behaves today*: unresolved human
  review threads block self-merge at the gate, but their reason string does
  NOT match the Greptile-fix router (the word "human" breaks the substring),
  so a human-only block routes to the generic proceed-with-session branch —
  see the ``NOTE(parity)`` in :func:`classify_self_merge_reasons`.

Design rules:

- The decision core is pure: no subprocess calls, no filesystem access.
- Bob-side policy (repo allowlists, score floors, GraphQL budget fuses) is
  NOT hardcoded here; callers pass it via :class:`LifecycleConfig` and the
  I/O adapter's environment.
- Where the bash behavior is ambiguous or arguably buggy, the port preserves
  it and marks the spot with a ``# NOTE(parity):`` comment. Behavior changes
  come later, with the brain-side switchover.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Item types that enable the self-merge fast path (lib.sh:534).
SELF_MERGE_TYPES: frozenset[str] = frozenset({"pr_update", "merge_ready"})

# Item type that enables the cross-repo Greptile status check (lib.sh:579).
CROSS_REPO_REVIEW_TYPE = "pr_update"

# Greptile confidence-score extraction — mirrors greptile-helper.sh:186
# jq: capture("Score[*:]*\\s*(?<n>[0-9])/5") (first match wins).
GREPTILE_SCORE_RE = re.compile(r"Score[*:]*\s*([0-9])/5")


# --- Decision types ---


class MergeLifecycleAction(Enum):
    """What the monitoring cycle should do next for a PR work item."""

    SELF_MERGE = "self_merge"  # attempt merge via the self-merge gate script
    SKIP_ITEM = "skip_item"  # self-merged — skip the LLM session entirely
    TRIGGER_REVIEW = "trigger_review"  # trigger Greptile via greptile-helper ONLY
    FIX_FINDINGS = "fix_findings"  # run a session with fix instructions injected
    WAIT = "wait"  # review in-flight / unknown state — no Greptile action
    PROCEED = "proceed"  # run the session with no Greptile injection
    NOT_APPLICABLE = "not_applicable"  # phase gating did not match this item


class InstructionKind(Enum):
    """Which fix-instruction template to inject into the session prompt.

    This enum is the seam between the decision port (step 1, contrib#1261)
    and the prompt-template port (step 2): decisions name a kind, and
    :func:`gptme_runloops.prompt_templates.render_instruction` renders its
    body.

    The first two members are produced by the merge-lifecycle decisions in
    this module. The ``GREPTILE_NEEDS_*`` members correspond to the
    ``build_item_investigate`` arms keyed by the poller's
    ``greptile_needs_fix`` / ``greptile_needs_improvement`` item types —
    rendered by the same template module but selected by item type, not by
    a decision here.
    """

    LOCAL_GREPTILE_FIX = "local_greptile_fix"  # lib.sh:425-470
    CROSS_REPO_GREPTILE_REFRESH = "cross_repo_greptile_refresh"  # lib.sh:474-517
    GREPTILE_NEEDS_FIX = "greptile_needs_fix"  # lib.sh:886-912
    GREPTILE_NEEDS_IMPROVEMENT = "greptile_needs_improvement"  # lib.sh:913-932


class SelfMergeBlockClass(Enum):
    """Classification of a not-eligible self-merge result's reasons."""

    NO_GREPTILE_REVIEW = "no_greptile_review"
    UNRESOLVED_THREADS = "unresolved_threads"
    SCORE_BELOW_FLOOR = "score_below_floor"
    OTHER = "other"


class AttemptDecision(Enum):
    """Outcome of the head-gated greptile convergence counter."""

    DISPATCH = "dispatch"  # dispatch a fix session; one attempt consumed
    DISPATCH_FREE = "dispatch_free"  # dispatch; no attempt consumed
    ESCALATE = "escalate"  # suppress dispatch; surface to a human


@dataclass(frozen=True)
class LifecycleDecision:
    """A single typed decision with its human-readable reason."""

    action: MergeLifecycleAction
    reason: str
    instructions: InstructionKind | None = None


@dataclass(frozen=True)
class WorkItem:
    """The grouped work item under consideration (repo, number, types)."""

    repo: str
    number: int | str
    types: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelfMergeCheckResult:
    """Parsed output of the self-merge gate (``self-merge-check.py --json``)."""

    eligible: bool
    reasons: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: str) -> SelfMergeCheckResult:
        """Parse the gate's ``--json`` output the way the bash caller does.

        Mirrors lib.sh:540 (``_json_get eligible False``) and lib.sh:550
        (reasons list, empty on parse failure). Fail-closed: any parse
        failure yields ``eligible=False`` with no reasons.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return cls(eligible=False, reasons=())
        if not isinstance(data, dict):
            return cls(eligible=False, reasons=())
        # NOTE(parity): the bash compares the *Python str()* of the JSON value
        # against "True" (_json_get prints `print(v)`), so JSON `true` AND the
        # JSON string "True" both count as eligible. Preserved exactly.
        eligible = str(data.get("eligible", False)) == "True"
        raw_reasons = data.get("reasons", [])
        reasons = (
            tuple(str(r) for r in raw_reasons) if isinstance(raw_reasons, list) else ()
        )
        return cls(eligible=eligible, reasons=reasons)


@dataclass(frozen=True)
class LifecycleConfig:
    """Caller-supplied policy. Bob-side values live in the brain, not here.

    Attributes:
        primary_repo: the agent's own workspace repo, excluded from the
            cross-repo Greptile check (lib.sh:579 hardcodes "ErikBjare/bob";
            the brain passes that value).
        greptile_repos_pattern: extended regex (``grep -qE`` semantics, i.e.
            re.search) selecting repos where the cross-repo Greptile
            lifecycle applies (lib.sh:577).
    """

    primary_repo: str
    greptile_repos_pattern: str


@dataclass
class LifecycleResult:
    """Outcome of one :func:`run_merge_lifecycle` pass over an item."""

    skip_item: bool = False
    instructions: InstructionKind | None = None
    decisions: list[LifecycleDecision] = field(default_factory=list)


# --- Pure decision core ---


def parse_greptile_score(body: str) -> int | None:
    """Extract the Greptile confidence score (0-5) from a summary-comment body.

    Mirrors greptile-helper.sh:186 — first ``Score[*:]*\\s*N/5`` match wins;
    returns ``None`` when no score is present.
    """
    m = GREPTILE_SCORE_RE.search(body)
    return int(m.group(1)) if m else None


def classify_self_merge_reasons(reasons: Sequence[str]) -> SelfMergeBlockClass:
    """Classify a not-eligible gate result's reasons (lib.sh:551-572).

    The bash greps the newline-joined reasons text, in priority order:
    "Greptile review not found" > "unresolved review thread" >
    ``Greptile score [0-9]/5 below floor``. First match wins even when other
    blocking reasons (e.g. red CI) are also present.

    NOTE(parity): the *human*-thread reason ("N unresolved human review
    thread(s) from: ...") does NOT contain the substring "unresolved review
    thread" (the word "human" sits inside it), so a PR blocked only by
    unresolved human threads classifies as OTHER: the session still runs
    (and the worker prompt tells it to answer every human comment) but no
    Greptile fix instructions are injected. Preserved as-is.
    """
    text = "\n".join(reasons)
    if "Greptile review not found" in text:
        return SelfMergeBlockClass.NO_GREPTILE_REVIEW
    if "unresolved review thread" in text:
        return SelfMergeBlockClass.UNRESOLVED_THREADS
    # lib.sh:563 — grep -qE "Greptile score [0-9]/5 below floor"
    if re.search(r"Greptile score [0-9]/5 below floor", text):
        return SelfMergeBlockClass.SCORE_BELOW_FLOOR
    return SelfMergeBlockClass.OTHER


def self_merge_gate_applicable(item: WorkItem, *, gate_available: bool = True) -> bool:
    """Phase-A gating (lib.sh:534): gate script present + pr_update/merge_ready."""
    return gate_available and bool(SELF_MERGE_TYPES & set(item.types))


def decide_self_merge_gate(
    item: WorkItem,
    check: SelfMergeCheckResult | None,
    *,
    gate_available: bool = True,
) -> LifecycleDecision:
    """Decide the self-merge phase (lib.sh:534-575) from an observed gate result.

    ``check`` is the parsed gate output (``None`` only when the phase is not
    applicable and the gate was never run).
    """
    if not self_merge_gate_applicable(item, gate_available=gate_available):
        return LifecycleDecision(
            MergeLifecycleAction.NOT_APPLICABLE,
            "self-merge phase skipped: gate script missing or item types "
            "lack pr_update/merge_ready",
        )
    if check is None:
        raise ValueError("self-merge phase applicable but no gate result provided")
    if check.eligible:
        # §7b invariant: self-merge only via the gate — SELF_MERGE is only
        # reachable through an eligible gate verdict.
        return LifecycleDecision(
            MergeLifecycleAction.SELF_MERGE,
            "self-merge gate reports eligible",
        )
    block = classify_self_merge_reasons(check.reasons)
    if block is SelfMergeBlockClass.NO_GREPTILE_REVIEW:
        # lib.sh:551-559 — trigger via helper, then proceed with the session
        # for any other updates (no fix instructions injected).
        return LifecycleDecision(
            MergeLifecycleAction.TRIGGER_REVIEW,
            "no Greptile review found — trigger via greptile-helper, then "
            "proceed with LLM session for any other updates",
        )
    if block is SelfMergeBlockClass.UNRESOLVED_THREADS:
        return LifecycleDecision(
            MergeLifecycleAction.FIX_FINDINGS,
            "Greptile has unresolved findings — spawn LLM session to address them",
            instructions=InstructionKind.LOCAL_GREPTILE_FIX,
        )
    if block is SelfMergeBlockClass.SCORE_BELOW_FLOOR:
        # lib.sh:563-570 — score floor blocks self-merge; dispatch a fix
        # session seeded with the summary-body findings (gptme#2987).
        return LifecycleDecision(
            MergeLifecycleAction.FIX_FINDINGS,
            "Greptile score below floor — spawn LLM session to address the "
            "summary-body findings",
            instructions=InstructionKind.LOCAL_GREPTILE_FIX,
        )
    # lib.sh:571-573 — first reason line only, empty string when no reasons.
    first_reason = check.reasons[0] if check.reasons else ""
    return LifecycleDecision(
        MergeLifecycleAction.PROCEED,
        f"not eligible for self-merge ({first_reason}), proceeding with LLM session",
    )


def cross_repo_review_applicable(
    item: WorkItem,
    config: LifecycleConfig,
    *,
    fix_instructions_pending: bool = False,
) -> bool:
    """Phase-B gating (lib.sh:579).

    Applies only when: no fix instructions are already queued, the item is a
    ``pr_update``, the repo is not the agent's primary workspace repo, and
    the repo matches the cross-repo Greptile pattern.

    NOTE(parity): the primary-repo exclusion is redundant with any sane
    pattern (the brain's pattern never matches ErikBjare/bob) but is a
    separate condition in the bash, so it is preserved as one here.
    """
    if fix_instructions_pending:
        return False
    if CROSS_REPO_REVIEW_TYPE not in item.types:
        return False
    if item.repo == config.primary_repo:
        return False
    # grep -qE semantics: unanchored search (patterns carry their own anchors).
    # NOTE(parity): `grep -qE` exits non-zero on an invalid pattern, which the
    # bash `if` treats as no-match — a malformed policy regex silently gates
    # the phase off, it never aborts the run. Mirror that instead of letting
    # re.error propagate.
    try:
        return re.search(config.greptile_repos_pattern, item.repo) is not None
    except re.error:
        logger.warning(
            "invalid greptile_repos_pattern %r — treating as no-match",
            config.greptile_repos_pattern,
        )
        return False


def classify_greptile_helper_status(status: str) -> LifecycleDecision:
    """Map a ``greptile-helper.sh status`` string to a decision (lib.sh:586-604).

    NOTE(parity): the bash arm for ``none`` is dead code — the helper's
    status command never prints "none" (it prints already-reviewed |
    needs-re-review | in-progress | awaiting-initial-review | stale |
    backoff). The arm is preserved because the port is behavior-identical,
    not because it is reachable.

    NOTE(parity): ``awaiting-initial-review`` and ``backoff`` fall into the
    unknown-state arm (skip). For awaiting-initial-review that is correct by
    policy (Greptile auto-reviews new PRs; never manually trigger initial
    reviews) but it is reached by fallthrough, not by an explicit arm.
    """
    if status in ("none", "stale"):
        return LifecycleDecision(
            MergeLifecycleAction.TRIGGER_REVIEW,
            f"cross-repo Greptile status '{status}' — triggering via greptile-helper",
        )
    if status == "needs-re-review":
        return LifecycleDecision(
            MergeLifecycleAction.FIX_FINDINGS,
            "cross-repo Greptile review is stale relative to the latest "
            "commits — injecting follow-up instructions",
            instructions=InstructionKind.CROSS_REPO_GREPTILE_REFRESH,
        )
    if status == "in-progress":
        return LifecycleDecision(
            MergeLifecycleAction.WAIT,
            "cross-repo Greptile review in progress — skipping trigger",
        )
    if status == "already-reviewed":
        return LifecycleDecision(
            MergeLifecycleAction.PROCEED,
            "cross-repo Greptile review clean",
        )
    return LifecycleDecision(
        MergeLifecycleAction.WAIT,
        f"unknown Greptile state: {status} — skipping",
    )


def decide_cross_repo_review(
    item: WorkItem,
    config: LifecycleConfig,
    status: str | None,
    *,
    fix_instructions_pending: bool = False,
    helper_available: bool = True,
) -> LifecycleDecision:
    """Decide the cross-repo Greptile phase (lib.sh:577-606).

    ``status`` is the helper's status output; pass ``None`` when the phase is
    gated off (it is ignored then). Callers running the real helper should
    map a failed status invocation to ``"in-progress"`` (fail-safe — the
    bash does this at lib.sh:584; :class:`SubprocessMergeLifecycleIO`
    replicates it).
    """
    if not cross_repo_review_applicable(
        item, config, fix_instructions_pending=fix_instructions_pending
    ):
        return LifecycleDecision(
            MergeLifecycleAction.NOT_APPLICABLE,
            "cross-repo Greptile phase skipped: fix instructions pending, "
            "not a pr_update, primary repo, or repo not in pattern",
        )
    if not helper_available:
        # lib.sh:580-581 — WARN and skip the check; the session still runs.
        return LifecycleDecision(
            MergeLifecycleAction.PROCEED,
            "greptile-helper.sh not found or not executable — skipping Greptile check",
        )
    if status is None:
        raise ValueError("cross-repo phase applicable but no helper status provided")
    return classify_greptile_helper_status(status)


# --- Head-gated convergence attempt counter (f1fce829fb) ---


def head_advanced(cur_head: str, last_attempt_head: str | None) -> bool:
    """True when this emit consumes convergence budget (an attempt = pushed commits).

    Mirrors pr-merge-health-poll.py:485-491: the first observation (no head
    tracked yet) counts, preserving the initial dispatch; afterwards only a
    head change counts. A re-emit at the same head means no fix landed
    (slot-starved dispatch or a no-op session) so we dispatch again without
    burning an attempt.
    """
    # NOTE(parity): an empty cur_head ("" — unparseable dedupe key) never
    # counts after the first observation, because `bool(cur_head)` gates the
    # comparison in the poller. The converse edge is also preserved: a stream
    # of ONLY empty-head emits counts on every emit (last_attempt_head stays
    # None because `cur_head or last_head` never stores ""), so a PR whose
    # dedupe keys never parse escalates on re-emits alone — identical
    # expression in pr-merge-health-poll.py:489-491,500. Unreachable in
    # practice (dedupe keys embed the probed head SHA) and pre-existing; a
    # real fix belongs upstream in the poller, not in this parity port.
    return last_attempt_head is None or (
        bool(cur_head) and cur_head != last_attempt_head
    )


def decide_greptile_attempt(
    entry: Mapping[str, Any],
    cur_head: str,
    max_attempts: int,
    now_iso: str,
    *,
    score: int | None = None,
    post_rebase: bool = False,
) -> tuple[AttemptDecision, dict[str, Any]]:
    """Head-gated per-PR convergence decision for one greptile_low_score emit.

    Behavior-identical port of the per-signal core of
    ``apply_convergence_cap`` (pr-merge-health-poll.py:430-502, commit
    f1fce829fb). ``entry`` is the PR's attempt-counter state (the poller's
    on-disk dict shape); the updated entry is returned alongside the
    decision and must be persisted by the caller.

    The *time-window* detection of post-rebase grace and the sweep-level
    counter reset stay in the poller; ``post_rebase`` arrives here as a
    computed boolean.
    """
    updated = dict(entry)

    if updated.get("escalated"):
        # Legacy escalations from before head-gated counting have no
        # ``last_attempt_head`` — they may have escalated purely on poll
        # re-emits that never dispatched a real fix session (incident
        # 2026-07-09: gptme-contrib#1253 escalated at count=3 with 0
        # commits). Reset those; real non-convergence re-escalates once
        # actual fix attempts exhaust the counter.
        if "last_attempt_head" in updated:
            return AttemptDecision.ESCALATE, updated
        # NOTE(parity): the reset clears count/escalated but deliberately
        # leaves first_attempt_at untouched (poll.py:443) — a later
        # re-escalation reports the original legacy start time, not the
        # start of the head-gated counter window.
        updated.update({"escalated": False, "escalated_at": None, "count": 0})

    count = int(updated.get("count", 0))

    # NOTE(parity): the cap is checked BEFORE the post-rebase grace
    # (poll.py:449-460), so a PR already at max_attempts escalates even when
    # this emit is the post-rebase re-review — the grace only applies while
    # budget remains. Preserved; changing the precedence is a behavior
    # change for the switchover to consider.
    if count >= max_attempts:
        updated.update(
            {
                "count": count,
                "last_score": score,
                "escalated": True,
                "escalated_at": now_iso,
                "last_attempt_at": now_iso,
            }
        )
        return AttemptDecision.ESCALATE, updated

    if post_rebase:
        # Post-rebase Greptile re-review — dispatch the fix session but do
        # not consume a convergence attempt slot (one-time grace).
        updated.update(
            {
                "last_score": score,
                "escalated": False,
                "escalated_at": None,
                "first_attempt_at": updated.get("first_attempt_at") or now_iso,
                "last_attempt_at": now_iso,
                "last_attempt_head": cur_head or updated.get("last_attempt_head"),
                "conflict_grace_used_at": now_iso,
            }
        )
        return AttemptDecision.DISPATCH_FREE, updated

    last_head = updated.get("last_attempt_head")
    counts_this_emit = head_advanced(cur_head, last_head)
    updated.update(
        {
            "count": count + 1 if counts_this_emit else count,
            "last_score": score,
            "escalated": False,
            "escalated_at": None,
            "first_attempt_at": updated.get("first_attempt_at") or now_iso,
            "last_attempt_at": now_iso,
            "last_attempt_head": cur_head or last_head,
        }
    )
    return (
        AttemptDecision.DISPATCH if counts_this_emit else AttemptDecision.DISPATCH_FREE,
        updated,
    )


# --- Thin I/O boundary ---


class MergeLifecycleIO(Protocol):
    """Injectable side-effect boundary for :func:`run_merge_lifecycle`.

    Implementations wrap the gh-backed scripts; the decision core above
    never touches a subprocess.
    """

    def self_merge_check(self, repo: str, number: int | str) -> SelfMergeCheckResult:
        """Run the self-merge gate and return its parsed verdict."""
        ...

    def self_merge(self, repo: str, number: int | str) -> bool:
        """Attempt the self-merge via the gate script; True on success."""
        ...

    def greptile_status(self, repo: str, number: int | str) -> str:
        """Return the greptile-helper status string ('in-progress' on failure)."""
        ...

    def trigger_review(self, repo: str, number: int | str) -> None:
        """Trigger a Greptile review via greptile-helper (anti-spam guarded)."""
        ...

    def promote_item_state(self, repo: str, number: int | str) -> None:
        """Advance the item's activity-gate state after a self-merge."""
        ...


def run_merge_lifecycle(
    item: WorkItem,
    config: LifecycleConfig,
    io: MergeLifecycleIO,
    *,
    gate_available: bool = True,
    helper_available: bool = True,
    fix_instructions_pending: bool = False,
    log: Callable[[str], None] | None = None,
) -> LifecycleResult:
    """Run the two-phase merge lifecycle for one work item.

    Orchestrates I/O in exactly the bash order (lib.sh:523-609):

    Phase A (self-merge fast path, lib.sh:534-575): when the gate script is
    available and the item is a pr_update/merge_ready, evaluate eligibility.
    Eligible → attempt the merge; success skips the LLM session entirely
    (returns ``skip_item=True``, mirroring the bash ``return 1``). Not
    eligible → route on the blocking reason (trigger review / queue local
    fix instructions / proceed).

    NOTE(parity): when the gate says eligible but the merge attempt fails,
    the bash falls through *silently* to Phase B — no message, no fix
    instructions, no retry. Preserved.

    Phase B (cross-repo Greptile check, lib.sh:577-606): when no fix
    instructions are queued and the item is a cross-repo pr_update in the
    configured pattern, consult the helper status and trigger / queue
    refresh instructions / wait accordingly.

    Returns the accumulated decisions plus the instruction kind (if any) the
    session prompt should inject.
    """
    emit = log or (lambda msg: logger.info("%s", msg))
    result = LifecycleResult()

    # --- Phase A: self-merge fast path (lib.sh:534-575) ---
    if self_merge_gate_applicable(item, gate_available=gate_available):
        emit("  Checking self-merge eligibility...")
        check = io.self_merge_check(item.repo, item.number)
        decision = decide_self_merge_gate(item, check, gate_available=gate_available)
        result.decisions.append(decision)

        if decision.action is MergeLifecycleAction.SELF_MERGE:
            if io.self_merge(item.repo, item.number):
                emit("  Self-merged! Skipping LLM session.")
                io.promote_item_state(item.repo, item.number)
                result.decisions.append(
                    LifecycleDecision(
                        MergeLifecycleAction.SKIP_ITEM,
                        "self-merge succeeded — skip the LLM session",
                    )
                )
                result.skip_item = True
                return result
            # NOTE(parity): failed merge attempt falls through silently.
        elif decision.action is MergeLifecycleAction.TRIGGER_REVIEW:
            if helper_available:
                emit(
                    f"  No Greptile review found. Triggering review on #{item.number}..."
                )
                io.trigger_review(item.repo, item.number)
                emit(
                    "  Review triggered. Proceeding with LLM session for any "
                    "other updates."
                )
            else:
                # lib.sh:557 — never trigger outside the helper (spam guard).
                emit(
                    "  WARN: greptile-helper.sh not found — skipping trigger to avoid spam"
                )
        elif decision.action is MergeLifecycleAction.FIX_FINDINGS:
            emit(f"  {decision.reason}")
            result.instructions = decision.instructions
        else:
            emit(f"  {decision.reason}")

    # --- Phase B: cross-repo Greptile lifecycle (lib.sh:577-606) ---
    pending = fix_instructions_pending or result.instructions is not None
    if cross_repo_review_applicable(item, config, fix_instructions_pending=pending):
        if not helper_available:
            decision = decide_cross_repo_review(
                item,
                config,
                None,
                fix_instructions_pending=pending,
                helper_available=False,
            )
            result.decisions.append(decision)
            emit(f"  WARN: {decision.reason}")
            return result
        emit(
            f"  [cross-repo] Checking Greptile status for {item.repo}#{item.number}..."
        )
        status = io.greptile_status(item.repo, item.number)
        decision = classify_greptile_helper_status(status)
        result.decisions.append(decision)
        emit(f"  [cross-repo] {decision.reason}")
        if decision.action is MergeLifecycleAction.TRIGGER_REVIEW:
            io.trigger_review(item.repo, item.number)
        elif decision.action is MergeLifecycleAction.FIX_FINDINGS:
            result.instructions = decision.instructions

    return result


@dataclass
class SubprocessMergeLifecycleIO:
    """Default I/O adapter wrapping the gh-backed scripts, mirroring the bash.

    All paths and policy env are caller-supplied (the brain passes its
    allowlists via ``env``); nothing Bob-specific is baked in.

    Attributes:
        self_merge_check_cmd: argv prefix for the gate check, e.g.
            ``["python3", ".../self-merge-check.py"]``. Invoked as
            ``<cmd> --json --repo <repo> <number>`` (lib.sh:537-539).
        self_merge_cmd: argv prefix for the merge script, invoked as
            ``<cmd> --repo <repo> <number>`` (lib.sh:543).
        greptile_helper: path to greptile-helper.sh; invoked as
            ``bash <helper> status|trigger <repo> <number>``.
        env: extra environment (e.g. WORKSPACE_REPO,
            SELF_MERGE_ALLOWED_PATHS) merged over os.environ for the gate
            and merge subprocesses.
        promote_state: optional hook called after a successful self-merge
            (the bash ``promote_item_state`` file shuffle stays brain-side
            in this step).
        timeout: per-subprocess timeout in seconds (None = no timeout,
            matching the bash, which sets none).
    """

    self_merge_check_cmd: Sequence[str]
    self_merge_cmd: Sequence[str]
    greptile_helper: str
    env: Mapping[str, str] | None = None
    promote_state: Callable[[str, int | str], None] | None = None
    timeout: float | None = None

    def _env(self) -> dict[str, str] | None:
        if self.env is None:
            return None
        import os

        merged = dict(os.environ)
        merged.update(self.env)
        return merged

    def self_merge_check(self, repo: str, number: int | str) -> SelfMergeCheckResult:
        # lib.sh:537-539: stderr discarded, failure tolerated (|| true) —
        # a failed check parses as not-eligible with no reasons.
        try:
            proc = subprocess.run(
                [*self.self_merge_check_cmd, "--json", "--repo", repo, str(number)],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=self.timeout,
            )
            raw = proc.stdout
        except (OSError, subprocess.SubprocessError):
            raw = ""
        return SelfMergeCheckResult.from_json(raw)

    def self_merge(self, repo: str, number: int | str) -> bool:
        # lib.sh:543: success is exit 0; stderr discarded.
        try:
            proc = subprocess.run(
                [*self.self_merge_cmd, "--repo", repo, str(number)],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=self.timeout,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def greptile_status(self, repo: str, number: int | str) -> str:
        # lib.sh:584: any failure fail-safes to "in-progress" (skip trigger).
        try:
            proc = subprocess.run(
                ["bash", self.greptile_helper, "status", repo, str(number)],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if proc.returncode != 0:
                return "in-progress"
            return proc.stdout.strip() or "in-progress"
        except (OSError, subprocess.SubprocessError):
            return "in-progress"

    def trigger_review(self, repo: str, number: int | str) -> None:
        # lib.sh:555/589: helper output passes through; failures non-fatal.
        try:
            proc = subprocess.run(
                ["bash", self.greptile_helper, "trigger", repo, str(number)],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if proc.stdout.strip():
                logger.info("%s", proc.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            logger.warning("greptile-helper trigger failed for %s#%s", repo, number)

    def promote_item_state(self, repo: str, number: int | str) -> None:
        if self.promote_state is not None:
            self.promote_state(repo, number)
