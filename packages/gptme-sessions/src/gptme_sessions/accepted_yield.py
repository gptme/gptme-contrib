"""Accepted-outcome yield per dispatch source.

Joins session records to host-supplied item outcomes and reports, per
``dispatch_cause.kind``, how many of the items those sessions worked on were
accepted by someone else.

The package only joins. Deciding whether an item was accepted is the host's
job (task trackers, PR ledgers, merger checks); the host hands those verdicts
in as data: JSONL rows of ``{"item": "<key>", "outcome": "<label>"}``.

Item keys, extracted per session in this order:

- ``task:<id>`` from ``dispatch_cause.task``;
- ``pr:<owner/repo#n>`` (lowercase) from ``dispatch_cause.pr``;
- otherwise ``pr:`` keys for the PR URLs in the session's ``deliverables``.

A session with none of these is *unkeyed*: it is counted as a coverage gap,
never as zero yield.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

DECIDED: tuple[str, ...] = ("accepted", "durable", "zero", "harmful", "closed")
"""Terminal outcomes. ``accepted`` = someone other than the agent acted on the
item; ``durable`` = the agent's own lasting work (self-merge, internal soak);
``zero``/``harmful``/``closed`` = terminal without acceptance."""

OUTCOMES: tuple[str, ...] = DECIDED + ("unverified", "pending")
"""Full vocabulary. ``unverified`` = e.g. merged with no verified merger;
``pending`` = not decided yet (also the default for an item with no row)."""

_PR_URL_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/([1-9][0-9]*)")


def _field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def session_items(record: Any) -> list[str]:
    """Item keys one session worked on; empty if the session is unkeyed.

    Accepts a ``SessionRecord`` or a raw record dict.
    """
    cause = _field(record, "dispatch_cause")
    cause = cause if isinstance(cause, Mapping) else {}
    if cause.get("task"):
        return [f"task:{cause['task']}"]
    if cause.get("pr"):
        return [f"pr:{str(cause['pr']).lower()}"]
    refs = {
        f"{o}/{r}#{n}".lower()
        for value in _field(record, "deliverables") or []
        if isinstance(value, str)
        for o, r, n in _PR_URL_RE.findall(value)
    }
    return [f"pr:{ref}" for ref in sorted(refs)]


def load_outcomes(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Index outcome rows by item; the last row for an item wins.

    Raises ``ValueError`` on a row with a missing item or an unknown label, so
    a producer bug fails loudly instead of silently counting as ``pending``.
    """
    index: dict[str, str] = {}
    for n, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise ValueError(f"outcome row {n}: expected a JSON object")
        item, outcome = row.get("item"), row.get("outcome")
        if not isinstance(item, str) or not item:
            raise ValueError(f"outcome row {n}: missing 'item'")
        if outcome not in OUTCOMES:
            raise ValueError(
                f"outcome row {n} ({item}): unknown outcome {outcome!r}; "
                f"expected one of {', '.join(OUTCOMES)}"
            )
        index[item] = outcome
    return index


def read_outcome_files(paths: Iterable[Path]) -> dict[str, str]:
    """Read JSONL outcome files in order; later files override earlier ones."""

    def rows() -> Iterable[Mapping[str, Any]]:
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        yield json.loads(line)

    return load_outcomes(rows())


def compute_accepted_yield(
    records: Iterable[Any], outcomes: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """Per-source report, ordered by session count (largest first).

    ``sessions_per_accepted`` divides *all* of the source's sessions, keyed or
    not, by accepted items: a cost ceiling, since unkeyed sessions may still
    have outcomes the join cannot see.
    """
    per_kind: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sessions": 0, "keyed": 0, "items": {}}
    )
    for record in records:
        cause = _field(record, "dispatch_cause")
        kind = str(cause.get("kind") or "unknown") if isinstance(cause, Mapping) else "unknown"
        bucket = per_kind[kind]
        bucket["sessions"] += 1
        items = session_items(record)
        if items:
            bucket["keyed"] += 1
        for item in items:
            bucket["items"][item] = outcomes.get(item, "pending")

    report: dict[str, dict[str, Any]] = {}
    for kind, b in sorted(per_kind.items(), key=lambda kv: (-kv[1]["sessions"], kv[0])):
        counts = Counter(b["items"].values())
        decided = sum(counts[o] for o in DECIDED)
        accepted = counts["accepted"]
        report[kind] = {
            "sessions": b["sessions"],
            "keyed_sessions": b["keyed"],
            "unkeyed_sessions": b["sessions"] - b["keyed"],
            "coverage": round(b["keyed"] / b["sessions"], 3),
            "items": len(b["items"]),
            "outcomes": {o: counts[o] for o in OUTCOMES if counts[o]},
            "decided": decided,
            "accepted_yield": round(accepted / decided, 3) if decided else None,
            "sessions_per_accepted": round(b["sessions"] / accepted, 1) if accepted else None,
        }
    return report


def format_accepted_yield(report: Mapping[str, Mapping[str, Any]], window: str) -> str:
    lines = [
        f"Accepted-outcome yield by dispatch source ({window}; items are distinct task/PR keys)",
        f"{'source':<14}{'sessions':>9}{'coverage':>10}{'items':>7}{'decided':>9}"
        f"{'yield':>8}{'s/acc':>7}  outcomes",
    ]
    for kind, r in report.items():
        yld = "n/a" if r["accepted_yield"] is None else f"{r['accepted_yield']:.0%}"
        spa = "-" if r["sessions_per_accepted"] is None else f"{r['sessions_per_accepted']:g}"
        outs = " ".join(f"{k}={v}" for k, v in r["outcomes"].items()) or "unkeyed"
        lines.append(
            f"{kind:<14}{r['sessions']:>9}{r['coverage']:>10.0%}{r['items']:>7}"
            f"{r['decided']:>9}{yld:>8}{spa:>7}  {outs}"
        )
    lines.append(
        "yield = accepted / decided; s/acc = all sessions per accepted item (ceiling); "
        "unkeyed sessions are coverage gaps, not zero yield"
    )
    return "\n".join(lines)
