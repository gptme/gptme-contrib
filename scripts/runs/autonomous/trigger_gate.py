#!/usr/bin/env python3
"""Composable trigger-gate framework for scheduled autonomous agent runs.

A *trigger gate* answers one question, fast (<15s) and with zero LLM calls:
*should a scheduled trigger fire a full agent session at all?* It is the pacing
layer that runs **before** dispatch — distinct from the runtime-admission chain
that decides, inside a dispatched run, whether the LLM should be invoked and
under what model/category.

This module is the shared *framework*, not any one agent's policy. Three
independent implementations (contrib's `session-gate.py`, Gordon's 616-LOC
`session-gate.py`, and Alice's inline bash chain) converged on the same shape;
this factors out what they agree on so a fork registers its own triggers instead
of copy-pasting a fourth divergent gate. See the reconciliation writeup in
alice#79 (shared-core-convergence) for the full derivation.

The pieces all three agreed on, and which live here:

- **Durable JSON state** — one file with `last_session_ts`, `blocked_until`,
  `last_reasons` audit trail. Gordon and contrib arrived at this independently
  (2-of-3), so it is the *required* state contract, not a nicety.
- **Uniform trigger signature** — `Trigger = (State) -> (fired, reason)`,
  accumulated into a list; `should_run = any fired`. Cleaner than contrib's
  monolithic `decide()` and than Alice's early-`exit 0` chain.
- **Suppressor concept** — some signals (usage-overpacing, a manually declared
  blocked window) suppress *non-critical* triggers without being triggers
  themselves. A trigger declares whether it is suppressible.
- **`max_skip` force-run floor** — a non-suppressible trigger that force-runs
  every N hours. The individual checks fail *toward skip*; the composite is
  fail-open because `max_skip` always runs. This is the safety valve that keeps
  a bug in one check from silently wedging the agent forever.

What stays *local policy* (do NOT put here): the specific set of triggers, the
threshold values, calendar rules (Sunday/late-night, NYSE holidays), the order,
bandit/exhaustion decisions. Register those as your own triggers/suppressors.

Exit-code convention for a CLI built on this framework (kept from the two
existing gates):
- 0 = skip this scheduled run
- 1 = at least one trigger fired; run the agent
- 2 = gate error
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

UTC = timezone.utc

# Durable state is a plain JSON object so any agent can read/write it without a
# schema dependency. Reserved keys the framework itself reads/writes:
#   last_session_ts : ISO-8601 UTC of the last run that actually happened
#   blocked_until   : ISO-8601 UTC; a manually declared blocked window
#   last_reasons    : audit trail of the last decision's (key, reason) pairs
#   last_decision   : "run" | "skip"
#   last_checked_at : ISO-8601 UTC of the last gate evaluation
State = dict[str, Any]

# A trigger inspects state and returns (fired, reason). A trigger MUST NOT raise
# for control flow — the composer treats any exception as (False, None), i.e.
# fail-toward-skip. Composite fail-open is provided by the max_skip floor.
Trigger = Callable[[State], "tuple[bool, str | None]"]

# A suppressor inspects state and returns (active, reason). While active, every
# *suppressible* trigger is skipped. Suppressors also fail-toward-inactive.
Suppressor = Callable[[State], "tuple[bool, str | None]"]


def parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None.

    Tolerates a trailing ``Z`` and naive timestamps (assumed UTC), matching the
    lenient parsing both existing gates use so state written by either is
    readable here.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def load_state(path: Path) -> State:
    """Load durable gate state, failing open to ``{}``.

    A missing or corrupt state file must never wedge the gate: an unreadable
    file returns empty state, so the max_skip floor will force a run rather than
    the agent going silent. This matches Gordon's ``load_state`` (bare except ->
    {}) and is deliberately more forgiving than contrib's old ``read_state``,
    which raised on corrupt JSON.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_state(path: Path, state: State) -> None:
    """Persist durable gate state (pretty, key-sorted, trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def mark_session_ran(state: State, now: datetime | None = None) -> State:
    """Record that a session just ran (call post-session, via ``--update-state``).

    Kept separate from evaluation so the observe-vs-commit split is explicit:
    the gate persists *observation* state on every check, but only advances
    ``last_session_ts`` when a run actually completed.
    """
    state["last_session_ts"] = (now or datetime.now(UTC)).isoformat()
    return state


def blocked_window(key: str = "blocked_until") -> Suppressor:
    """A suppressor for a manually declared blocked window.

    The agent sets ``state[key]`` (ISO-8601 UTC) when all actionable work is
    gated behind a future event; until then, suppressible triggers are held so
    the gate doesn't spawn hourly no-op sessions. Critical (non-suppressible)
    triggers — inbox, max_skip — still fire.
    """

    def _suppressor(state: State) -> tuple[bool, str | None]:
        until = parse_dt(state.get(key))
        if until is None:
            return False, None
        now = datetime.now(UTC)
        if now < until:
            hours = (until - now).total_seconds() / 3600
            return True, f"blocked until {state[key]} ({hours:.1f}h remaining)"
        return False, None

    return _suppressor


def max_skip_trigger(hours: float, ts_key: str = "last_session_ts") -> Trigger:
    """The non-suppressible force-run floor.

    Force a run if no session was recorded within ``hours``. If no session was
    ever recorded, force a run. Register this with ``suppressible=False`` so it
    fires through any suppressor — this is what makes the *composite* gate
    fail-open even though individual triggers fail-toward-skip.
    """

    def _trigger(state: State) -> tuple[bool, str | None]:
        last = parse_dt(state.get(ts_key))
        if last is None:
            return True, "no previous session recorded — forcing run"
        hours_since = (datetime.now(UTC) - last).total_seconds() / 3600
        if hours_since >= hours:
            return True, f"max skip exceeded: {hours_since:.1f}h since last session"
        return False, None

    return _trigger


@dataclass
class TriggerSpec:
    """A trigger plus whether suppressors may hold it.

    ``suppressible=True`` (the default) means an active suppressor skips this
    trigger. Critical triggers — the inbox (a message may carry an
    authorization) and the max_skip floor — set ``suppressible=False`` so they
    always evaluate.
    """

    key: str
    fn: Trigger
    suppressible: bool = True


@dataclass
class GateResult:
    should_run: bool
    reasons: list[tuple[str, str]] = field(default_factory=list)
    suppressed_by: list[tuple[str, str]] = field(default_factory=list)

    def apply_to_state(self, state: State, now: datetime | None = None) -> State:
        """Write the decision's audit trail into durable state.

        Does NOT advance ``last_session_ts`` — that is ``mark_session_ran``'s
        job, called only after a run actually completes.
        """
        state["last_checked_at"] = (now or datetime.now(UTC)).isoformat()
        state["last_decision"] = "run" if self.should_run else "skip"
        state["last_reasons"] = [{"key": k, "reason": r} for k, r in self.reasons]
        return state


def _safe_eval(
    fn: Callable[[State], tuple[bool, str | None]], state: State
) -> tuple[bool, str | None]:
    """Evaluate a trigger/suppressor, failing toward (False, None) on error."""
    try:
        return fn(state)
    except Exception:  # noqa: BLE001 — deliberate fail-toward-skip
        return False, None


def run_gate(
    state: State,
    triggers: list[TriggerSpec],
    suppressors: list[tuple[str, Suppressor]] | None = None,
) -> GateResult:
    """Evaluate the gate: return whether to run, and why.

    1. Evaluate each suppressor (fail-toward-inactive). Collect the active ones.
    2. For each trigger: if it is suppressible and any suppressor is active, skip
       it. Otherwise evaluate it (fail-toward-skip).
    3. ``should_run`` is true iff at least one trigger fired.

    The suppressor set never blocks a non-suppressible trigger, so the max_skip
    floor (and inbox) always get their say — the same "some signals suppress,
    some can't be suppressed" structure all three gates express, made first-class.
    """
    suppressors = suppressors or []
    active_suppressors: list[tuple[str, str]] = []
    for key, supp in suppressors:
        active, reason = _safe_eval(supp, state)
        if active:
            active_suppressors.append((key, reason or key))

    fired: list[tuple[str, str]] = []
    for spec in triggers:
        if spec.suppressible and active_suppressors:
            continue
        triggered, reason = _safe_eval(spec.fn, state)
        if triggered:
            fired.append((spec.key, reason or spec.key))

    return GateResult(
        should_run=bool(fired),
        reasons=fired,
        suppressed_by=active_suppressors,
    )
