"""Dispatcher tick records and the "bindable covers granted while behind" metric.

A *dispatcher tick* is one fire of an agent's session dispatcher (a fanout timer,
a worker sweep, a run loop). Each tick appends one JSON line. The shared fields
are the minimum needed to ask: **when the account is behind its usage target,
did the dispatcher have enough bound work to fill the capacity it was given?**

Shared row contract (one JSON object per line):

``ts``
    ISO-8601 timestamp with a timezone.
``granted``
    Session slots this tick could start: the width after the resource/quota gate
    narrowed the configured maximum. This is what the account can absorb, so it
    is the coverage target.
``bindable``
    Work items (tasks, program instances, scout jobs) the dispatcher could bind
    to a slot this tick. ``null`` means "not computed", never zero.
``pacing``
    The account's position against its linear usage target: ``"behind"``,
    ``"on-pace"``, ``"ahead"`` or ``"unknown"``. ``null`` is unknown.

Agents may add their own fields (spawned counts, skip reasons, ...); readers
ignore them.

Why ``granted`` and not the number of slots the dispatcher *requested*: an agent
that only requests one slot per ready category can show full coverage of its own
request while leaving most of the granted capacity idle. Coverage of the request
hides exactly the starvation this metric exists to catch.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

BEHIND = "behind"


@dataclass(frozen=True)
class DispatchTick:
    ts: datetime
    granted: int | None
    bindable: int | None
    pacing: str | None

    @classmethod
    def from_dict(cls, row: dict) -> DispatchTick | None:
        """Parse a row; return None when ``ts`` is missing or unparseable."""
        try:
            ts = datetime.fromisoformat(str(row["ts"]))
        except (KeyError, ValueError):
            return None
        if ts.tzinfo is None:
            return None
        pacing = row.get("pacing")
        return cls(
            ts=ts,
            granted=_int_or_none(row.get("granted")),
            bindable=_int_or_none(row.get("bindable")),
            pacing=pacing if isinstance(pacing, str) and pacing else None,
        )


def _int_or_none(value: object) -> int | None:
    # bool is an int subclass; a stray true/false is not a count.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def append_tick(
    path: Path,
    *,
    granted: int | None,
    bindable: int | None,
    pacing: str | None,
    extra: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Append one tick row to ``path`` and return the row written."""
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    row = {
        **(extra or {}),
        "ts": ts,
        "granted": granted,
        "bindable": bindable,
        "pacing": pacing,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def read_ticks(path: Path, since: datetime | None = None) -> Iterator[DispatchTick]:
    """Yield parseable ticks from ``path``, skipping malformed lines."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            tick = DispatchTick.from_dict(row)
            if tick is None or (since is not None and tick.ts < since):
                continue
            yield tick


@dataclass(frozen=True)
class BindableCoverage:
    """Share of behind-pace ticks whose bindable work covered the granted slots.

    ``share`` is ``covered / measured``; ``None`` when nothing was measurable.
    Ticks that cannot be judged are counted, not dropped silently:
    ``unknown_pacing`` (no pacing verdict), ``unknown_bindable`` (behind, but
    bindable not computed) and ``no_capacity`` (behind, but granted was 0 or
    unknown, so there was nothing to fill).
    """

    ticks: int
    behind: int
    measured: int
    covered: int
    unknown_pacing: int
    unknown_bindable: int
    no_capacity: int

    @property
    def share(self) -> float | None:
        return self.covered / self.measured if self.measured else None

    def to_dict(self) -> dict:
        return {**asdict(self), "share": self.share}


def bindable_coverage(ticks: Iterable[DispatchTick]) -> BindableCoverage:
    total = behind = measured = covered = 0
    unknown_pacing = unknown_bindable = no_capacity = 0
    for tick in ticks:
        total += 1
        if tick.pacing in (None, "unknown"):
            unknown_pacing += 1
            continue
        if tick.pacing != BEHIND:
            continue
        behind += 1
        if tick.granted is None or tick.granted <= 0:
            no_capacity += 1
            continue
        if tick.bindable is None:
            unknown_bindable += 1
            continue
        measured += 1
        if tick.bindable >= tick.granted:
            covered += 1
    return BindableCoverage(
        ticks=total,
        behind=behind,
        measured=measured,
        covered=covered,
        unknown_pacing=unknown_pacing,
        unknown_bindable=unknown_bindable,
        no_capacity=no_capacity,
    )
