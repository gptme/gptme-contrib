"""Composable runtime-admission gate functions for autonomous run loops.

This is the *runtime-admission* abstraction (does this already-triggered session
deserve to spend inference?), which is distinct from the *trigger* gate in
``session-gate.py`` (should a scheduled slot fire at all?). See the reconciliation
in alice's ``knowledge/infrastructure/gate-chain-reconciliation.md`` — the two
things called "gate" are different abstractions and get different libraries.

Design contract (per the shared-core arc's mechanism-vs-policy rule):

* Each gate is a **pure function** ``(state) -> GateDecision`` — no I/O of its own.
  Expensive or side-effecting inputs (a state-delta subprocess, a quota probe) are
  passed in as **callables** so the gate stays testable and only pays for a check
  when its own logic actually needs it.
* The gate owns the **mechanism** (the decision table). The caller owns the
  **policy**: which streak counter feeds it, the threshold values, the order of the
  chain, and the bypass calendar. A thin agent-local wrapper composes these gates.

The first gate extracted here is :func:`state_delta_gate`, lifted byte-for-byte
from alice's ``autonomous-run-cc.sh`` state-delta pre-gate. Its decision table is
pinned by contract tests so the eventual cut-over from bash is provably
behaviour-preserving.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class GateDecision:
    """Outcome of a runtime-admission gate.

    ``proceed`` is the admission decision; ``reason`` is a human-readable
    explanation suitable for logging (it mirrors the bash gate's echo lines so
    logs read identically before and after the cut-over).
    """

    proceed: bool
    reason: str


def state_delta_gate(
    *,
    noop_only_streak: int,
    quota_behind_pace: bool,
    changes_detected: Callable[[], bool],
    force_session: bool = False,
) -> GateDecision:
    """Skip a session when the last one no-op'd and nothing has changed since.

    Mechanism (identical decision table to alice's ``autonomous-run-cc.sh`` stage):

    1. **Gate inactive** when the session is forced or the noop-only streak is 0 —
       there is no reason to suppress a session that isn't part of a noop run.
    2. **Quota bypass** — if quota is materially behind pace, proceed regardless, to
       keep utilisation catch-up unblocked. Evaluated *before* the change check so
       the (cheap but non-free) ``changes_detected`` probe is skipped on bypass,
       preserving the bash evaluation order.
    3. **Change check** — only now is ``changes_detected`` called. If something
       changed (new commits, messages, standups, settlement events), proceed.
    4. Otherwise **skip**: the session would almost certainly be a status-check noop.

    Policy stays with the caller: *which* streak counter is passed as
    ``noop_only_streak`` (alice deliberately uses NOOP_ONLY_STREAK, not NOOP_STREAK,
    so runtime failures bypass the gate and reach the fail-guard), the pace-gap
    threshold that sets ``quota_behind_pace``, and what ``changes_detected`` probes.

    Args:
        noop_only_streak: Consecutive *clean* (non-failure) noop sessions. ``>= 1``
            arms the gate.
        quota_behind_pace: True when quota is behind pace enough to justify running
            anyway (the caller applies its own threshold, e.g. > 0.25).
        changes_detected: Zero-arg callable returning True if state changed since the
            last session. Called at most once, and only when the gate is armed and
            quota is not bypassing — mirroring ``state-delta.py --check``.
        force_session: When True the gate is inactive (manual/forced runs always
            proceed).

    Returns:
        A :class:`GateDecision`.
    """
    if force_session or noop_only_streak < 1:
        return GateDecision(True, "gate inactive (no noop-only streak)")
    if quota_behind_pace:
        return GateDecision(
            True, "bypassing: quota behind pace — proceeding to improve utilization"
        )
    if changes_detected():
        return GateDecision(True, "changes detected despite noop streak — proceeding")
    return GateDecision(
        False,
        f"no state changes since last session (noop-only streak: {noop_only_streak}) — skipping",
    )
