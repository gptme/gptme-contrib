"""Composable *trigger-gate* framework for autonomous run loops.

This is the **trigger** abstraction — *"should a scheduled slot fire at all?"* —
answered fast (<15s), with zero LLM cost, *before* a run is dispatched. It is
distinct from the *runtime-admission* gate in :mod:`gptme_runloops.gates`
(*"does this already-triggered session deserve to spend inference?"*). See the
reconciliation in alice's ``knowledge/infrastructure/gate-chain-reconciliation.md``:
the two things called "gate" are different abstractions and get different libraries.

The reference implementation for this abstraction is Gordon's
``scripts/runs/session-gate.py`` (a live, battle-hardened trigger gate), corroborated
by contrib's older ``session-gate.py`` (durable-state idea) — two independent agents
converging on the same shape, which is why it is promoted here from convention to a
**required state contract**.

Design contract (per the shared-core arc's mechanism-vs-policy rule):

* A **trigger** is a pure function ``(TriggerState) -> (fired: bool, reason: str | None)``.
  This uniform signature (Gordon's) is cleaner than Alice's early-``exit 0`` chain and
  than contrib's monolithic ``decide()``.
* Triggers declare whether they are **suppressible**. A *suppressor* (e.g. "quota is
  over-pace", "inside a backoff window") can veto suppressible triggers, but
  **non-suppressible** triggers (inbox, settlement, the ``max_skip`` floor) always run.
  This makes first-class the "some signals suppress, some can't be suppressed" structure
  that Alice's chain expresses only implicitly via ordering.
* Each trigger **fails toward skip** (a raised exception is caught and treated as
  ``(False, None)``), but :func:`max_skip_trigger` force-fires every ``max_skip_hours``
  regardless — so the *composite* is fail-open even though individual checks are
  fail-closed (Gordon's ``max_skip`` floor / Bob's "force-run safety valve").
* State is a single durable JSON file with ``blocked_until`` and a ``last_reasons`` audit
  trail. Observation vs. commit is a clean split: the gate persists *observation* state;
  advancing ``last_session_ts`` is a separate post-session call (:func:`mark_session`).
* A trigger or suppressor may be marked **shadow** (Bob's autonomous-gate.sh "shadow
  mode"): it is evaluated against live state exactly as it would be if enforced, but its
  verdict does *not* affect the fire/skip outcome — it is recorded separately in
  :class:`TriggerDecision` (``shadow_fired`` / ``shadow_suppressed``) so a new gate can be
  soaked against real traffic before it is flipped on. This is how a fork adds a gate
  without risking a wedge on day one.

Domain triggers (Gordon's settlement/NYSE checks, Alice's Sunday/late-night calendar)
are **not** part of this library — they register as local plugins. The library owns the
framework: the uniform signature, the suppressor concept, the durable state, and the
``max_skip`` floor.

This first cut ships the pieces both agents already agree on semantically — the durable
:class:`TriggerState` contract, the :func:`max_skip_trigger` floor, and shadow mode — plus
the :func:`evaluate` composer that wires triggers and suppressors together. Remaining
domain triggers follow as local plugins; the decision table here is pinned by contract
tests so the eventual cut-over from the bash/inline gates is provably behaviour-preserving.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# A trigger answers: did something happen that justifies firing this slot?
#   returns (fired, reason). reason is a short human-readable log line when fired.
Trigger = Callable[["TriggerState"], "tuple[bool, str | None]"]

# A suppressor answers: should suppressible triggers be vetoed right now?
#   returns (suppressed, reason). reason explains the veto for the audit trail.
Suppressor = Callable[["TriggerState"], "tuple[bool, str | None]"]


@dataclass(frozen=True)
class TriggerState:
    """Durable observation state for the trigger gate.

    This is the **required state contract** for the trigger-gate abstraction —
    two independent agents (Gordon, contrib) arrived at the same shape.

    * ``last_session_ts`` — epoch seconds of the last *dispatched* session. Advanced
      only by :func:`mark_session` (the observe-vs-commit split), never by the gate.
    * ``blocked_until`` — epoch seconds; while ``now < blocked_until`` the gate is in a
      backoff window and suppressible triggers are vetoed (see :func:`in_blocked_window`).
    * ``last_reasons`` — the reasons from the most recent *fire* decision, for a durable
      audit trail (contrib's field; answers "why did the last run happen?" after reboot).
    * ``observations`` — free-form observation slots a fork's domain triggers read/write
      (Gordon stores ``last_balance``/``last_prices`` here). The framework never
      interprets these; it only round-trips them.
    """

    last_session_ts: float = 0.0
    blocked_until: float = 0.0
    last_reasons: tuple[str, ...] = ()
    observations: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_session_ts": self.last_session_ts,
            "blocked_until": self.blocked_until,
            "last_reasons": list(self.last_reasons),
            "observations": dict(self.observations),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TriggerState:
        return cls(
            last_session_ts=float(data.get("last_session_ts", 0.0) or 0.0),
            blocked_until=float(data.get("blocked_until", 0.0) or 0.0),
            last_reasons=tuple(data.get("last_reasons", ()) or ()),
            observations=dict(data.get("observations", {}) or {}),
        )


def load_state(path: str | Path) -> TriggerState:
    """Load durable trigger state, failing open to a fresh state.

    A missing file (fresh fork, first run) or a corrupt/unreadable one must never
    wedge the lane — both degrade to a default :class:`TriggerState`, which lets the
    ``max_skip`` floor take over. This is the Tier-0 rule: no silent wedge on bad state.
    """
    p = Path(path)
    try:
        return TriggerState.from_dict(json.loads(p.read_text()))
    except (OSError, ValueError):
        return TriggerState()


def save_state(path: str | Path, state: TriggerState) -> None:
    """Persist trigger state atomically (write-temp-then-rename).

    Atomicity matters: a torn write on this single file would corrupt every future
    gate decision. The temp file lives beside the target so the rename is same-filesystem.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    tmp.replace(p)


def in_blocked_window(state: TriggerState, *, now: float | None = None) -> bool:
    """True while the gate is inside a ``blocked_until`` backoff window."""
    now = time.time() if now is None else now
    return now < state.blocked_until


def max_skip_trigger(
    state: TriggerState,
    *,
    max_skip_hours: float,
    now: float | None = None,
) -> tuple[bool, str | None]:
    """Force-fire the slot if it has been skipped for longer than ``max_skip_hours``.

    This is the composite's fail-open floor: however many suppressors are active and
    however many individual triggers fail closed, a slot that has gone dark for
    ``max_skip_hours`` fires anyway. A never-run state (``last_session_ts == 0``) counts
    as long-overdue and fires immediately, so a fresh fork is never wedged waiting for a
    baseline. This trigger is **non-suppressible** by construction (callers should mark
    it so) — a backoff window must not be able to suppress the safety valve.
    """
    now = time.time() if now is None else now
    elapsed_h = (now - state.last_session_ts) / 3600.0
    if elapsed_h < 0:
        # A future ``last_session_ts`` (clock skew, a cross-machine state write, or a
        # corrupt/manual edit) makes ``elapsed_h`` negative, so the ``>=`` check below
        # would silently never fire — wedging the gate forever. ``max_skip`` is the
        # fail-open safety valve, so it must never be the thing that goes quiet: treat
        # a future timestamp as invalid and force a run.
        return (
            True,
            f"max_skip: last_session_ts is {-elapsed_h:.1f}h in the future "
            "(clock skew or corrupt state) — forcing run",
        )
    if elapsed_h >= max_skip_hours:
        return (
            True,
            f"max_skip: {elapsed_h:.1f}h since last session (floor {max_skip_hours:.0f}h)",
        )
    return False, None


@dataclass(frozen=True)
class TriggerSpec:
    """A registered trigger plus whether a suppressor may veto it.

    ``suppressible=False`` marks a signal that always runs regardless of backoff/quota
    suppressors (inbox, settlement, the ``max_skip`` floor). ``suppressible=True`` marks
    a signal that a suppressor may veto (routine work-signal checks).

    ``shadow=True`` marks a trigger being *soaked*: it is evaluated under the same rules as
    a live trigger, but a fire is recorded in :attr:`TriggerDecision.shadow_fired` instead
    of contributing to ``should_run``. Flip it to ``shadow=False`` once the soak shows it
    fires when (and only when) it should.
    """

    name: str
    check: Trigger
    suppressible: bool = True
    shadow: bool = False


@dataclass(frozen=True)
class SuppressorSpec:
    """A registered suppressor plus whether it is being soaked in shadow mode.

    ``shadow=True`` evaluates the suppressor but does *not* let its veto affect the fire
    decision — an active shadow suppressor is recorded in
    :attr:`TriggerDecision.shadow_suppressed` only. This is the safest gate to soak: a new
    suppressor is the thing that can *stop* runs, so you want to see how often it would
    have vetoed before it actually can.

    :func:`evaluate` also accepts a bare ``(name, suppressor)`` tuple for back-compat; such
    an entry is treated as a live (non-shadow) suppressor.
    """

    name: str
    check: Suppressor
    shadow: bool = False


@dataclass(frozen=True)
class TriggerDecision:
    """Outcome of a trigger-gate evaluation.

    ``should_run`` is the fire decision; ``reasons`` is the ``(name, reason)`` list that
    justified it (empty when skipping); ``suppressed`` records which *live* suppressors
    were active, for the audit trail even on a run.

    ``shadow_fired`` / ``shadow_suppressed`` carry the counterfactual verdicts of any
    shadow-mode triggers/suppressors — what they *would* have done had they been live.
    They never affect ``should_run``; they exist so a soak can be tallied. Both are empty
    when nothing is being soaked.
    """

    should_run: bool
    reasons: tuple[tuple[str, str], ...]
    suppressed: tuple[tuple[str, str], ...]
    shadow_fired: tuple[tuple[str, str], ...] = ()
    shadow_suppressed: tuple[tuple[str, str], ...] = ()


def _run_check(check: Trigger, state: TriggerState) -> tuple[bool, str | None]:
    """Call a trigger, failing toward skip on any exception (fail-closed per check)."""
    try:
        return check(state)
    except Exception:  # noqa: BLE001 - a broken domain trigger must never wedge the gate
        return False, None


def _as_suppressor_spec(
    entry: SuppressorSpec | tuple[str, Suppressor],
) -> SuppressorSpec:
    """Normalize a suppressor entry, accepting bare ``(name, suppressor)`` for back-compat."""
    if isinstance(entry, SuppressorSpec):
        return entry
    name, suppressor = entry
    return SuppressorSpec(name=name, check=suppressor)


def evaluate(
    triggers: Iterable[TriggerSpec],
    state: TriggerState,
    *,
    suppressors: Iterable[SuppressorSpec | tuple[str, Suppressor]] = (),
) -> TriggerDecision:
    """Compose triggers and suppressors into a single fire/skip decision.

    Semantics (Gordon's, made first-class):

    1. Run every suppressor. Each active *live* veto records into ``suppressed``; an active
       *shadow* veto records into ``shadow_suppressed`` instead and does not gate anything.
    2. If any live suppressor is active, only **non-suppressible** triggers are evaluated;
       otherwise every trigger is evaluated. (Shadow suppressors never gate triggers — that
       is the whole point of soaking them.)
    3. Each evaluated trigger runs fail-closed (an exception → not fired).
    4. ``should_run`` is true iff at least one **live** trigger fired. Live fires become
       ``reasons`` (and the caller should persist them to ``TriggerState.last_reasons`` via
       :func:`record_decision`); shadow fires become ``shadow_fired`` and never set
       ``should_run``.

    The composite is fail-open provided a non-suppressible :func:`max_skip_trigger` is
    among ``triggers`` — that guarantees an eventual fire no matter what suppressors do.

    Suppressor entries may be :class:`SuppressorSpec` (to mark ``shadow=True``) or a bare
    ``(name, suppressor)`` tuple (treated as live).
    """
    active_suppressors: list[tuple[str, str]] = []
    shadow_suppressed: list[tuple[str, str]] = []
    for entry in suppressors:
        sup = _as_suppressor_spec(entry)
        try:
            suppressed, reason = sup.check(state)
        except Exception:  # noqa: BLE001 - a broken suppressor must not veto everything
            continue
        if suppressed:
            pair = (sup.name, reason or sup.name)
            (shadow_suppressed if sup.shadow else active_suppressors).append(pair)

    any_suppressed = bool(active_suppressors)
    fired: list[tuple[str, str]] = []
    shadow_fired: list[tuple[str, str]] = []
    for spec in triggers:
        if any_suppressed and spec.suppressible:
            continue
        did_fire, reason = _run_check(spec.check, state)
        if did_fire:
            pair = (spec.name, reason or spec.name)
            (shadow_fired if spec.shadow else fired).append(pair)

    return TriggerDecision(
        should_run=bool(fired),
        reasons=tuple(fired),
        suppressed=tuple(active_suppressors),
        shadow_fired=tuple(shadow_fired),
        shadow_suppressed=tuple(shadow_suppressed),
    )


def record_decision(state: TriggerState, decision: TriggerDecision) -> TriggerState:
    """Return a copy of ``state`` with ``last_reasons`` set from ``decision``.

    Persists the audit trail without advancing ``last_session_ts`` — that is
    :func:`mark_session`'s job (the observe-vs-commit split). Reasons are stored on both
    fire and skip so "why did (or didn't) the last slot fire?" survives a reboot.
    """
    reasons = tuple(f"{name}: {reason}" for name, reason in decision.reasons)
    return replace(state, last_reasons=reasons)


def mark_session(state: TriggerState, *, now: float | None = None) -> TriggerState:
    """Return a copy of ``state`` with ``last_session_ts`` advanced to ``now``.

    Called *post-session* (a separate invocation from the gate itself) so the
    ``max_skip`` clock only resets when a run actually happened.
    """
    now = time.time() if now is None else now
    return replace(state, last_session_ts=now)
