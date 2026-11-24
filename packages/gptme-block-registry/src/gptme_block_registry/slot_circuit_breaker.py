"""Per-slot circuit breaker — shared credential-survival reaction layer.

Extracted from an agent's ``scripts/runs/autonomous/slot_circuit_breaker.py``
so that any agent can use the same breaker state without importing
fleet-internal modules.  The original module stays in the owning agent's repo
as a thin CLI wrapper and caller; this package owns the portable logic.

Design
------
The spawn loop calls :func:`decide_respawn` before each spawn, feeding it the
previous attempt's verdict:

* :data:`AUTH_DEATH` — the previous worker died on a transient / rotated-OAuth
  401.  Record a failure; suppress respawn once the threshold is reached.
* :data:`PRODUCTIVE` — the previous worker ran and produced output.  Record a
  success (resets / closes the breaker).
* :data:`NEUTRAL` — no prior signal (new work item).  Check-only: use current
  state to decide suppression but record neither success nor failure, so a
  consecutive-death run survives unrelated spawns.

While the breaker is OPEN (or half-open with no probe sent yet) an
:data:`AUTH_DEATH` / :data:`PRODUCTIVE` verdict predates the breaker and is
treated as stale: it is check-only like :data:`NEUTRAL` — it never closes or
re-opens the breaker, and once the cooldown has elapsed the call itself is the
single half-open probe. Its outcome decides (PRODUCTIVE closes, AUTH_DEATH
re-opens). Callers with multiple queued/in-flight attempts must supply
``verdict_started_at`` (the attempt's wall-clock start, not its completion or
file modification time) so another retry's stale evidence cannot settle the
probe. Omitting it retains the legacy serialized-caller contract: the next
non-neutral verdict MUST be the granted probe's outcome.

**Why NEUTRAL matters**: without it, every brand-new work item (with no prior
attempt) looks like a successful run that resets the consecutive-failure count
to zero, neutering the breaker mid-storm.

Cross-process state
-------------------
The spawn loop is bash + short-lived Python, so counters cannot live in process
memory.  They are persisted to a JSON file under the agent's ``state/``
directory and guarded by an ``fcntl`` advisory lock so concurrent spawn cycles
don't race the counter.

The circuit breaker uses wall-clock timestamps throughout (not ``monotonic``),
so hydrated state remains valid across process boundaries without clock-domain
bridging.

Tier 0 — filesystem + stdlib only.  No external dependencies.

Design reference: knowledge/technical-designs/block-registry-credential-survival-sublayer.md
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Three-way verdict constants
# ---------------------------------------------------------------------------

AUTH_DEATH = "auth_death"
"""Previous attempt died on a transient 401 — record a failure."""

PRODUCTIVE = "productive"
"""Previous attempt ran and produced output — record a success."""

NEUTRAL = "neutral"
"""No prior signal (new work item) — check-only, record nothing."""

_VERDICTS = (AUTH_DEATH, PRODUCTIVE, NEUTRAL)

# ---------------------------------------------------------------------------
# Defaults — callers may override; there are no agent-repo-specific paths here
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLD = 3
"""Consecutive auth-deaths before the breaker opens."""

DEFAULT_COOLDOWN = 300.0
"""Seconds a slot stays suppressed before a half-open probe is allowed."""


# ---------------------------------------------------------------------------
# Minimal stdlib circuit-breaker state machine (wall-clock, cross-process)
# ---------------------------------------------------------------------------


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _SlotBreaker:
    """Wall-clock circuit breaker for cross-process persistence.

    Intentionally not thread-safe — the caller serialises access via the
    ``fcntl`` lock in :func:`_locked_state`.
    """

    def __init__(self, failure_threshold: int, cooldown: float) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._opened_at: float | None = None
        self._probe_pending: bool = False
        self._probe_started_at: float | None = None
        self._probe_requires_timestamp: bool = False
        # Grant time of the probe that last closed the breaker. Verdicts from
        # attempts that started before it predate the recovery and are stale.
        self._recovered_at: float | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def is_open(self, now: float) -> bool:
        """Return True if this call should be suppressed."""
        if self._state == CircuitState.CLOSED:
            return False
        if self._state == CircuitState.OPEN:
            # A future opened_at means the wall clock stepped back; trusting it
            # would stretch the cooldown without bound, so allow the probe.
            if self._opened_at is not None and (
                self._opened_at > now or (now - self._opened_at) >= self.cooldown
            ):
                self._state = CircuitState.HALF_OPEN
                self._probe_pending = True
            else:
                return True
        # HALF_OPEN: exactly one probe allowed — consume the slot on first call
        if self._probe_pending or (
            # The granted probe never reported (killed worker, finished item,
            # dropped dispatch). Re-grant after a cooldown so the slot cannot
            # stay dark forever; the lost probe's late verdict is then stale.
            self._probe_started_at is not None
            and not now - self.cooldown < self._probe_started_at <= now
        ):
            self._probe_pending = False
            self._probe_started_at = now
            return False
        return True

    def record_failure(self, now: float) -> None:
        if self._state == CircuitState.HALF_OPEN:
            # Probe failed → re-open and reset cooldown
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._probe_started_at = None
            self._probe_requires_timestamp = False
        else:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = now

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN and self._probe_started_at is not None:
            self._recovered_at = self._probe_started_at
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None
        self._probe_pending = False
        self._probe_started_at = None
        self._probe_requires_timestamp = False


# ---------------------------------------------------------------------------
# Cross-process persistence helpers
# ---------------------------------------------------------------------------


@contextmanager
def _locked_state(state_file: Path) -> Iterator[dict[str, Any]]:
    """Yield the mutable per-slot state dict under an exclusive file lock.

    Locking uses a sibling ``.lock`` file rather than the state file itself,
    so the state file can be replaced atomically (temp file + rename) without
    invalidating an in-progress flock — renaming over a locked fd's own inode
    would silently detach the lock from the new file. This also means a
    reader that opens the state file directly (without taking the lock) never
    observes a truncated or partially written file.
    Fail-safe: a corrupt / missing file yields an empty dict rather than raising.
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.with_name(state_file.name + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            try:
                raw = state_file.read_text(errors="replace")
            except OSError:
                raw = ""
            try:
                data = json.loads(raw or "{}")
                if not isinstance(data, dict):
                    data = {}
            except (json.JSONDecodeError, ValueError):
                data = {}
            yield data
            out = json.dumps(data, indent=2, sort_keys=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(state_file.parent), prefix=f".{state_file.name}."
            )
            try:
                with os.fdopen(fd, "w") as tmp_file:
                    tmp_file.write(out)
                os.replace(tmp_name, state_file)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _load_breaker(
    slot_state: dict[str, Any],
    threshold: int,
    cooldown: float,
) -> _SlotBreaker:
    """Hydrate a :class:`_SlotBreaker` from persisted wall-clock JSON.

    Any malformed field resets the whole slot to CLOSED rather than crashing
    dispatch — a corrupt state file must never brick the spawn loop.
    """
    b = _SlotBreaker(failure_threshold=threshold, cooldown=cooldown)
    try:
        b._failure_count = int(slot_state.get("failure_count", 0))
        try:
            b._state = CircuitState[str(slot_state.get("state", "CLOSED")).upper()]
        except KeyError:
            b._state = CircuitState.CLOSED
        opened_at = slot_state.get("opened_at")
        if (
            b._state in (CircuitState.OPEN, CircuitState.HALF_OPEN)
            and opened_at is not None
        ):
            b._opened_at = float(opened_at)
        if b._state == CircuitState.OPEN and (
            b._opened_at is None or not math.isfinite(b._opened_at)
        ):
            # Without a usable opened_at the cooldown can never elapse.
            raise ValueError(f"OPEN slot with unusable opened_at: {opened_at!r}")
        probe_pending = slot_state.get("probe_pending", False)
        if not isinstance(probe_pending, bool):
            raise ValueError(f"non-bool probe_pending: {probe_pending!r}")
        b._probe_pending = probe_pending
        probe_started_at = slot_state.get("probe_started_at")
        b._probe_started_at = (
            float(probe_started_at) if probe_started_at is not None else None
        )
        if b._probe_started_at is not None and not math.isfinite(b._probe_started_at):
            b._probe_started_at = None
        requires_timestamp = slot_state.get("probe_requires_timestamp", False)
        if not isinstance(requires_timestamp, bool):
            raise ValueError("non-bool probe_requires_timestamp")
        b._probe_requires_timestamp = requires_timestamp
        recovered_at = slot_state.get("recovered_at")
        b._recovered_at = float(recovered_at) if recovered_at is not None else None
        if b._recovered_at is not None and not math.isfinite(b._recovered_at):
            b._recovered_at = None
    except Exception:
        b._state = CircuitState.CLOSED
        b._failure_count = 0
        b._opened_at = None
        b._probe_pending = False
        b._probe_started_at = None
        b._probe_requires_timestamp = False
        b._recovered_at = None
    return b


def _save_breaker(b: _SlotBreaker) -> dict[str, Any]:
    """Serialise a :class:`_SlotBreaker` to a wall-clock persistable dict."""
    out: dict[str, object] = {
        "failure_count": b._failure_count,
        "state": b._state.name,
        "opened_at": b._opened_at,
        "probe_pending": b._probe_pending,
        "probe_started_at": b._probe_started_at,
        "probe_requires_timestamp": b._probe_requires_timestamp,
        "recovered_at": b._recovered_at,
    }
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decide_respawn(
    *,
    state_file: Path,
    slot: str,
    verdict: str = NEUTRAL,
    verdict_started_at: float | None = None,
    threshold: int = DEFAULT_THRESHOLD,
    cooldown: float = DEFAULT_COOLDOWN,
) -> tuple[bool, str]:
    """Decide whether the spawn loop may respawn a worker on *slot*.

    Parameters
    ----------
    state_file:
        Path to the JSON file holding per-slot breaker state (caller supplies;
        typically ``workspace / "state" / "slot-circuit-breaker.json"``).
    slot:
        The credential slot identity (e.g. ``"arm-a"``, ``"arm-b"``).  Callers
        resolve the active slot; this function is policy-free.
    verdict:
        The previous attempt's three-way outcome — :data:`AUTH_DEATH`,
        :data:`PRODUCTIVE`, or :data:`NEUTRAL`.  Default is ``NEUTRAL`` (safe
        for new work items that have no prior signal).
    verdict_started_at:
        Wall-clock start of the attempt producing the verdict. Supply for every
        non-neutral verdict when retry items or workers overlap. A verdict from
        before the probe grant is check-only. Once a timestamped call grants a
        probe, missing timestamps cannot settle it either. Without timestamps,
        callers must serialize attempts and submit only the probe's outcome
        while HALF_OPEN; the library cannot infer attempt identity.
    threshold:
        Consecutive auth-deaths before the breaker opens.
    cooldown:
        Seconds the slot stays suppressed before a half-open probe is allowed.

    Returns
    -------
    (suppress, breaker_state):
        ``suppress`` is ``True`` when the breaker is open and the respawn
        should be skipped.  ``breaker_state`` is the post-decision state name
        for logging.
    """
    if verdict not in _VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}; expected one of {_VERDICTS}")

    with _locked_state(state_file) as data:
        # Sample after acquiring the lock: a pre-lock timestamp could accept
        # evidence from an attempt that started while we waited for the grant.
        now = time.time()
        if verdict_started_at is not None and (
            not math.isfinite(verdict_started_at) or verdict_started_at > now
        ):
            raise ValueError("verdict_started_at must be finite and not in the future")
        slot_state = data.get(slot, {})
        b = _load_breaker(slot_state, threshold, cooldown)

        suppress = _apply_verdict(b, verdict, now, verdict_started_at)

        data[slot] = _save_breaker(b)
        return suppress, b._state.name


def _apply_verdict(
    b: _SlotBreaker,
    verdict: str,
    now: float,
    verdict_started_at: float | None = None,
) -> bool:
    """Feed *verdict* to *b*, return whether to suppress the respawn."""

    def check_only() -> bool:
        suppress = b.is_open(now)
        if not suppress and b.state == CircuitState.HALF_OPEN:
            b._probe_requires_timestamp = verdict_started_at is not None
        return suppress

    if verdict == NEUTRAL:
        # Check-only: advance the cooldown clock but record nothing.
        return check_only()

    if (
        b.state == CircuitState.CLOSED
        and verdict_started_at is not None
        and b._recovered_at is not None
        and verdict_started_at < b._recovered_at
    ):
        # The attempt started before the probe that proved the slot healthy.
        return check_only()

    if b.state == CircuitState.HALF_OPEN and (
        (b._probe_requires_timestamp and verdict_started_at is None)
        or (
            verdict_started_at is not None
            and (
                b._probe_started_at is None or verdict_started_at < b._probe_started_at
            )
        )
    ):
        # Another queued retry or pre-probe worker cannot settle the probe.
        return check_only()

    # Capture state *before* any side effects — is_open() transitions
    # OPEN→HALF_OPEN as a side effect, which must not influence verdict routing.
    state = b._state

    if state == CircuitState.OPEN or (
        state == CircuitState.HALF_OPEN and b._probe_pending
    ):
        # No probe has been sent since the breaker opened, so an AUTH_DEATH or
        # PRODUCTIVE verdict describes an attempt from before it opened: stale
        # evidence that must neither close nor re-open the breaker. Treat it as
        # check-only (like NEUTRAL): suppress within the cooldown; once the
        # cooldown has elapsed the call itself becomes the probe. Suppressing
        # unconditionally here would leave the breaker open forever whenever
        # every queued item is a retry carrying a prior verdict (only a
        # brand-new NEUTRAL item could advance it).
        return check_only()

    if verdict == AUTH_DEATH:
        if state == CircuitState.HALF_OPEN:
            # Probe was sent and returned AUTH_DEATH → re-open.
            b.record_failure(now)
            return True
        # CLOSED → record the failure.
        b.record_failure(now)
        return b.is_open(now)

    # PRODUCTIVE
    if state == CircuitState.HALF_OPEN:
        # Probe was sent and returned PRODUCTIVE → close.
        b.record_success()
        return False
    # CLOSED
    b.record_success()
    return False
