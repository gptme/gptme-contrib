"""Deepen ladder: find work when the queue is dry, instead of idling or starting noop sessions.

Tracking: gptme/gptme-contrib#2310 (principles 1 and 2).

A dispatcher tick that is behind its capacity target and has fewer bindable items
than slots runs the ladder. Rungs run **in order** until the deficit is filled.
Each rung is a source of candidate work items, ordered by the host from cheapest
and most trusted (own open work needing action) to heaviest (deeper work on the
best open item). If every rung comes back empty, the result says so with
per-rung zero counts, and the caller idles on purpose.

Same contract as :mod:`gptme_runloops.gates`: this module owns the mechanism,
the host owns the policy.

* A rung is a ``produce`` callable that returns candidate items (and an optional
  note). It is called only when the deficit is still open, so expensive rungs
  cost nothing on a tick an earlier rung already satisfied.
* Filing is the host's ``file_items`` callback. It receives at most the remaining
  deficit and returns how many it actually filed (dedupe may drop some).
* No I/O here: no task format, no ledger path, no GitHub client.

The companion :func:`bound_session_gate` enforces principle 1: a work session
starts only when it holds a bound item (a task, a program instance, or a scout
job with its own output contract).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from .gates import GateDecision

T = TypeVar("T")


@dataclass(frozen=True)
class RungOutput(Generic[T]):
    """What one rung produced: candidate items plus an optional human note."""

    items: Sequence[T] = ()
    note: str = ""


@dataclass(frozen=True)
class Rung(Generic[T]):
    """One ordered source of candidate work.

    ``produce`` runs only while the deficit is open. Raise nothing for "empty":
    return an empty :class:`RungOutput` with a note saying why (for example
    ``"unavailable: producer not built"``), so the zero is visible in the log.
    """

    name: str
    produce: Callable[[], RungOutput[T]]


@dataclass(frozen=True)
class RungResult:
    """Per-rung counts. ``emitted`` candidates, ``filed`` after the host's dedupe."""

    emitted: int
    filed: int
    note: str = ""
    error: str = ""


@dataclass
class LadderResult(Generic[T]):
    """Outcome of one ladder run.

    ``rungs`` holds every rung that ran, zero counts included, in run order.
    Rungs after the one that closed the deficit do not run and are absent.
    ``produced_by`` is the first rung that filed anything, or ``"none"``.
    """

    deficit: int
    rungs: dict[str, RungResult] = field(default_factory=dict)
    filed: list[T] = field(default_factory=list)
    produced_by: str = "none"

    @property
    def remaining(self) -> int:
        return max(self.deficit - len(self.filed), 0)

    @property
    def idle_on_purpose(self) -> bool:
        """True when the deficit was open, every rung ran, and none filed work."""
        return self.deficit > 0 and not self.filed

    def summary(self) -> str:
        """One log line: ``produced_by=X own-prs=1/3 miners=0/0 ...`` (filed/emitted)."""
        counts = " ".join(f"{k}={v.filed}/{v.emitted}" for k, v in self.rungs.items())
        return f"produced_by={self.produced_by} {counts}".rstrip()


def run_ladder(
    rungs: Sequence[Rung[T]],
    deficit: int,
    file_items: Callable[[str, Sequence[T]], int],
) -> LadderResult[T]:
    """Run ``rungs`` in order until ``deficit`` items are filed.

    ``file_items(rung_name, items)`` receives at most the remaining deficit and
    returns the number it filed; it is not called with an empty list. A rung
    whose ``produce`` raises is recorded with ``error`` and the ladder moves on:
    one broken source must not starve the rest. A ``file_items`` failure is
    not caught, because the caller cannot tell what was filed.

    The filed items are reported as the first ``filed`` items handed to
    ``file_items``; a host whose dedupe drops items from the middle should
    re-derive identities from its own store.
    """
    if deficit < 0:
        raise ValueError(f"deficit must be >= 0, got {deficit}")
    result: LadderResult[T] = LadderResult(deficit=deficit)
    for rung in rungs:
        if result.remaining <= 0:
            break
        if rung.name in result.rungs:
            raise ValueError(f"duplicate rung name: {rung.name!r}")
        try:
            out = rung.produce()
        except Exception as exc:  # one broken source must not starve the rest
            result.rungs[rung.name] = RungResult(
                emitted=0, filed=0, error=f"{type(exc).__name__}: {exc}"
            )
            continue
        items = list(out.items)
        take = items[: result.remaining]
        filed = file_items(rung.name, take) if take else 0
        if filed < 0 or filed > len(take):
            raise ValueError(
                f"file_items for {rung.name!r} returned {filed}, expected 0..{len(take)}"
            )
        result.rungs[rung.name] = RungResult(
            emitted=len(items), filed=filed, note=out.note
        )
        if filed:
            if result.produced_by == "none":
                result.produced_by = rung.name
            result.filed.extend(take[:filed])
    return result


def bound_session_gate(bound_item: str | None) -> GateDecision:
    """Admit a work session only when it holds a bound item.

    ``bound_item`` is the host's identity for what the session will work on
    (a task id, a program instance, a scout job). ``None`` or an empty string
    means the dispatcher found nothing: the caller should run the ladder or
    idle, not start a session that will discover the queue is dry.
    """
    if bound_item and bound_item.strip():
        return GateDecision(True, f"bound to {bound_item.strip()}", cause="bound")
    return GateDecision(
        False,
        "no bound item: run the deepen ladder or idle on purpose",
        cause="unbound",
    )
