"""Runtime-admission gates: composable checks that decide whether a *dispatched*
autonomous run should proceed.

## Two abstractions, both called "gate"

There are two distinct gate abstractions in the fleet, and conflating them is a
recurring source of confusion (see the gate-chain reconciliation in Alice's
brain, `knowledge/infrastructure/gate-chain-reconciliation.md`):

- **Trigger gate** — *"should a scheduled trigger fire at all?"* Runs before a
  run is even dispatched. Reference implementations: contrib's
  ``scripts/runs/autonomous/session-gate.py`` and Gordon's richer live variant.
- **Runtime-admission gate** (this module) — *"should this specific dispatched
  run proceed, and under what model/category?"* Runs inside the run, after
  dispatch, before the LLM is invoked. Alice is the only source of these today;
  her chain is ~330 lines of inline bash in ``scripts/autonomous-run-cc.sh``.

This module extracts the *mechanism* of Alice's runtime-admission chain into
composable, testable functions so a forked agent gets them for free. The
*policy* — the order of the chain, the threshold values, the calendar rules,
bandit category selection — stays in each agent's thin wrapper. This mirrors the
mechanism/policy split the shared-core arc already uses for credential-slots.

Each gate is a pure function ``(state) -> GateDecision``. State that requires I/O
(reading session records, running a subprocess) is injected as a callable so the
gate itself stays pure and trivially testable, and so its side effects only fire
on the branches where the live chain actually invokes them.
"""

from dataclasses import dataclass
from typing import Callable

__all__ = ["GateDecision", "state_delta_gate"]


@dataclass(frozen=True)
class GateDecision:
    """Outcome of a runtime-admission gate.

    ``proceed=True`` means this gate does not block the run — either it passed or
    it does not apply to the current run. ``proceed=False`` means the run should
    be skipped. ``reason`` is a human-readable audit string mirroring what the
    bash chain echoes today, so the structured decision and the log line stay in
    sync.
    """

    proceed: bool
    reason: str


def state_delta_gate(
    *,
    noop_only_streak: int,
    has_changes: Callable[[], bool],
    force_session: bool = False,
    quota_behind_pace: bool = False,
) -> GateDecision:
    """Skip a run when the last session was a noop and nothing has changed since.

    This is the extracted mechanism of Alice's state-delta pre-gate (gate #6 in
    her chain). It prevents the #1 noop cause: "status check only" sessions that
    spend minutes in the LLM just to conclude nothing changed, without blocking
    utilization catch-up.

    The decision, matching the live bash chain exactly:

    1. If ``force_session`` or ``noop_only_streak < 1`` → **proceed** (the gate
       does not apply). ``has_changes`` is *not* called.
    2. Else if ``quota_behind_pace`` → **proceed** (bypass to improve
       utilization). ``has_changes`` is *not* called — matching the bash, which
       skips the change check on this branch so its side effects (e.g.
       persisting the current "missing set") do not fire when bypassing.
    3. Else call ``has_changes()``: **proceed** if it returns ``True`` (changes
       detected despite the noop streak), otherwise **skip**.

    Args:
        noop_only_streak: Consecutive noop sessions that were *not* runtime
            failures. Uses the noop-only streak (not the raw noop streak) so an
            exit-127/auth outage bypasses this gate and reaches the failure
            guard — otherwise a silent outage locks the loop (a run failure
            bumps the streak, the gate skips before the LLM is invoked, no new
            failure is recorded, and the fail-guard never escalates).
        has_changes: Callable returning ``True`` when state has changed since the
            last session. Injected (rather than reading state internally) so the
            gate stays pure and the check's own side effects only fire on the
            branch where the live chain invokes them. For Alice this wraps
            ``state-delta.py --check`` (exit 0 → changes → ``True``).
        force_session: When ``True``, the gate never blocks (operator override).
        quota_behind_pace: When ``True``, subscription quota is materially behind
            pace, so the run proceeds to catch up regardless of state changes.

    Returns:
        A :class:`GateDecision`.
    """
    if force_session or noop_only_streak < 1:
        return GateDecision(
            proceed=True,
            reason="state-delta-gate: not applicable (force_session or no noop-only streak)",
        )
    if quota_behind_pace:
        return GateDecision(
            proceed=True,
            reason="state-delta-gate: bypassing — quota behind pace, proceeding to improve utilization",
        )
    if has_changes():
        return GateDecision(
            proceed=True,
            reason="state-delta-gate: changes detected despite noop streak — proceeding",
        )
    return GateDecision(
        proceed=False,
        reason=(
            "state-delta-gate: no state changes since last session "
            f"(noop-only streak: {noop_only_streak}) — skipping"
        ),
    )
