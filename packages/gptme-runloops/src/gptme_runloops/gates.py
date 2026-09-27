"""Composable runtime-admission gate functions for autonomous run loops.

This is the *runtime-admission* abstraction (does this already-triggered session
deserve to spend inference?), which is distinct from the *trigger* gate in
``session-gate.py`` (should a scheduled slot fire at all?). See the reconciliation
in the ``gate-chain-reconciliation`` design note — the two things called "gate"
are different abstractions and get different libraries.

Design contract (per the shared-core arc's mechanism-vs-policy rule):

* Each gate is a **pure function** ``(state) -> GateDecision`` — no I/O of its own.
  Expensive or side-effecting inputs (a state-delta subprocess, a quota probe) are
  passed in as **callables** so the gate stays testable and only pays for a check
  when its own logic actually needs it.
* The gate owns the **mechanism** (the decision table). The caller owns the
  **policy**: which streak counter feeds it, the threshold values, the order of the
  chain, and the bypass calendar. A thin agent-local wrapper composes these gates.

The first gate extracted here is :func:`state_delta_gate`, lifted byte-for-byte
from the reference ``autonomous-run-cc.sh`` state-delta pre-gate. Its decision table is
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

    Mechanism (identical decision table to the reference ``autonomous-run-cc.sh`` stage):

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
    ``noop_only_streak`` (the reference caller deliberately uses NOOP_ONLY_STREAK, not NOOP_STREAK,
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


@dataclass(frozen=True)
class StreakCooldownDecision:
    """Outcome of the unproductive-streak cooldown gate.

    ``proceed``/``reason`` mirror :class:`GateDecision`. ``toggle_action`` carries
    the skip-toggle side-effect the caller must apply *after* acting on the
    decision (the gate is pure and never touches the filesystem):

    * ``"set"`` — arm the toggle (this attempt runs; the next one in the toggle
      regime should skip). The bash ``touch /tmp/…-noop-skip-toggle``.
    * ``"clear"`` — disarm the toggle (this attempt skips; the next one should
      attempt). The bash ``rm -f`` of the same file.
    * ``None`` — leave the toggle untouched (every branch outside the toggle
      regime).
    """

    proceed: bool
    reason: str
    toggle_action: str | None = None


def streak_cooldown_gate(
    *,
    noop_streak: int,
    scaled_threshold: int,
    toggle_threshold: int,
    cooldown_multiplier: int,
    elapsed_seconds: int | None,
    quota_behind_pace: bool,
    toggle_armed: bool,
    force_session: bool = False,
) -> StreakCooldownDecision:
    """Back off after consecutive unproductive sessions (noop or failed).

    Mechanism (identical decision table to the reference ``autonomous-run-cc.sh``
    ``--- Unproductive streak cooldown ---`` stage), a two-tier progressive backoff:

    1. **Gate inactive** when the session is forced, or the effective streak is
       below ``toggle_threshold`` — nothing to back off from.
    2. **Scaled-cooldown regime** (``noop_streak >= scaled_threshold``): enforce a
       time-based cooldown of ``min(noop_streak * cooldown_multiplier, 12)`` hours
       since the last session.

       * ``elapsed_seconds is None`` (no last-session timestamp) → proceed; the bash
         falls through the whole block when it can't read a timestamp.
       * still inside the cooldown window → **skip**, unless ``quota_behind_pace``
         bypasses it to keep utilisation catch-up unblocked (checked first, matching
         the bash order).
       * cooldown elapsed → proceed.
    3. **Toggle regime** (``toggle_threshold <= noop_streak < scaled_threshold``):
       skip every *other* attempt. If the toggle is already armed, **skip** and
       clear it; otherwise **proceed** and arm it (so the next attempt skips).

    Policy stays with the caller (matching the bash, which resolves these from the
    weekday/weekend calendar before calling): the two thresholds, the cooldown
    multiplier, *which* streak counter feeds ``noop_streak`` (the reference uses
    ``NOOP_STREAK_EFFECTIVE`` — raw ``NOOP_STREAK`` minus auth-outage failures once a
    dead credential is repaired), the pace-gap threshold behind ``quota_behind_pace``,
    and reading/writing the toggle file that backs ``toggle_armed``/``toggle_action``.

    Args:
        noop_streak: Consecutive unproductive sessions (the caller's effective count).
        scaled_threshold: Streak at/above which the scaled time-cooldown applies
            (reference: 5 weekday, 3 weekend).
        toggle_threshold: Streak at/above which the skip-every-other toggle applies
            (reference: 3 weekday, 2 weekend). Must be ``<= scaled_threshold``.
        cooldown_multiplier: Multiplies the streak-hours cooldown (reference: 1
            weekday, 2 weekend).
        elapsed_seconds: Seconds since the last session, or ``None`` if unknown
            (no readable timestamp) — treated as "proceed", per the bash fall-through.
        quota_behind_pace: True when quota is behind pace enough to bypass the
            cooldown (caller applies its own threshold, e.g. > 0.25).
        toggle_armed: Whether the skip-toggle is currently set (the file exists).
        force_session: When True the gate is inactive (manual/forced runs proceed).

    Returns:
        A :class:`StreakCooldownDecision`.
    """
    if force_session or noop_streak < toggle_threshold:
        return StreakCooldownDecision(
            True, "gate inactive (streak below toggle threshold)"
        )

    if noop_streak >= scaled_threshold:
        cooldown_hours = min(noop_streak * cooldown_multiplier, 12)
        cooldown_seconds = cooldown_hours * 3600
        if elapsed_seconds is None:
            return StreakCooldownDecision(
                True, "no last-session timestamp — proceeding"
            )
        if elapsed_seconds < cooldown_seconds:
            if quota_behind_pace:
                return StreakCooldownDecision(
                    True,
                    "bypassing: quota behind pace — proceeding to improve utilization",
                )
            return StreakCooldownDecision(
                False,
                f"{noop_streak} consecutive unproductive sessions, last {elapsed_seconds}s ago "
                f"— waiting for {cooldown_hours}h cooldown",
            )
        return StreakCooldownDecision(True, "cooldown elapsed — proceeding")

    # Toggle regime: toggle_threshold <= noop_streak < scaled_threshold.
    if toggle_armed:
        return StreakCooldownDecision(
            False,
            f"{noop_streak} consecutive unproductive sessions — skipping (will retry next cycle)",
            toggle_action="clear",
        )
    return StreakCooldownDecision(
        True,
        f"{noop_streak} consecutive unproductive sessions — attempting "
        "(next will skip if still unproductive)",
        toggle_action="set",
    )
