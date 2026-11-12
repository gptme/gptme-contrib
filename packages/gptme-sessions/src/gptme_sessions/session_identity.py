"""Canonical session identity across the three session-id formats.

Session records carry three id shapes (2026-11-11 frontier review, J2):

* 36-char UUIDs (current runs, PM workers),
* 8-hex ids (gptme short ids; unique across the full history),
* 4-hex autonomous labels (``4033``), which recur across days: 2,023 of
  17,566 distinct labels in the archives appear on more than one row.

Side ledgers (``pr-outcomes.jsonl``, ``merge-deliverables.jsonl``,
grade revisions) reference sessions by any of these, sometimes by a record's
``session_label`` or ``cc_session_uuid`` instead of its ``session_id``. Every
analysis used to re-implement that join. :class:`SessionIndex` does it once:
exact ids and aliases resolve directly; a 4-hex label resolves only with an
event time, to the nearest record within a window, and an ambiguous or
out-of-window label returns ``None`` rather than a guess.

Read-only and stdlib only. Originally written in Bob's metaproductivity package.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX8_RE = re.compile(r"^[0-9a-f]{8}$")
_LABEL4_RE = re.compile(r"^[0-9a-f]{4}$")

# Record fields that name the same session as ``session_id``. Not
# ``trajectory_owner_session_id`` / ``parent_session_id``: those name a
# *different* (owning) session.
ALIAS_FIELDS = ("cc_session_uuid", "session_label")


def id_format(session_id: object) -> str:
    """``uuid`` | ``hex8`` | ``label4`` | ``other`` | ``missing``."""
    sid = str(session_id or "").strip().lower()
    if not sid:
        return "missing"
    if _UUID_RE.match(sid):
        return "uuid"
    if _HEX8_RE.match(sid):
        return "hex8"
    if _LABEL4_RE.match(sid):
        return "label4"
    return "other"


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def record_time(row: dict) -> datetime | None:
    """Session start if recorded, else the record timestamp (UTC-aware)."""
    return _parse_ts(row.get("start_time")) or _parse_ts(row.get("timestamp"))


def canonical_session_key(row: dict) -> str:
    """Stable key for one session record.

    UUID and 8-hex ids are already unique and pass through lower-cased. A 4-hex
    label is qualified with its UTC start date (``4033@2026-11-12``); same-day
    label collisions are rare but possible and are not disambiguated here.
    """
    sid = str(row.get("session_id") or "").strip().lower()
    if id_format(sid) == "label4":
        dt = record_time(row)
        if dt is not None:
            return f"{sid}@{dt.astimezone(UTC).date().isoformat()}"
    return sid


class SessionIndex:
    """Resolve any session reference to a session record.

    ``resolve(ref, at=...)``: exact ``session_id`` match first, then aliases
    (``cc_session_uuid``, ``session_label``). When a reference matches several
    records (always possible for 4-hex labels), ``at`` picks the record whose
    time is nearest to ``at`` within ``window`` before it and ``slack`` after
    (records without ``start_time`` are stamped at session end);
    without ``at``, or with none in range, the result is ``None``.
    """

    def __init__(
        self,
        rows: Iterable[dict],
        *,
        window: timedelta = timedelta(days=2),
        slack: timedelta = timedelta(hours=3),
    ) -> None:
        self.window = window
        self.slack = slack
        self._by_id: dict[str, list[dict]] = defaultdict(list)
        self._by_alias: dict[str, list[dict]] = defaultdict(list)
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict) or id(row) in seen:
                continue
            seen.add(id(row))
            sid = str(row.get("session_id") or "").strip().lower()
            if sid:
                self._by_id[sid].append(row)
            for field in ALIAS_FIELDS:
                alias = str(row.get(field) or "").strip().lower()
                if alias and alias != sid:
                    self._by_alias[alias].append(row)
        self.stats: Counter[str] = Counter()

    def _pick(self, candidates: list[dict], at: datetime | None) -> dict | None:
        if at is None:
            self.stats["ambiguous_no_time"] += 1
            return None
        in_range = []
        for row in candidates:
            dt = record_time(row)
            if dt is None:
                continue
            if at - self.window <= dt <= at + self.slack:
                in_range.append((abs((at - dt).total_seconds()), row))
        if not in_range:
            self.stats["out_of_window"] += 1
            return None
        in_range.sort(key=lambda pair: pair[0])
        if len(in_range) > 1 and in_range[0][0] == in_range[1][0]:
            self.stats["ambiguous_time"] += 1
            return None
        return in_range[0][1]

    def resolve(self, ref: object, at: object = None) -> dict | None:
        key = str(ref or "").strip().lower()
        if not key:
            self.stats["missing"] += 1
            return None
        when = _parse_ts(at)
        fmt = id_format(key)
        for source, table in (("id", self._by_id), ("alias", self._by_alias)):
            candidates = table.get(key)
            if not candidates:
                continue
            # Unique non-label ids need no time check: an 8-hex/UUID id names one
            # session even when the event is days later (merge after review).
            if fmt != "label4" and len(candidates) == 1:
                self.stats[f"{source}:{fmt}"] += 1
                return candidates[0]
            row = self._pick(candidates, when)
            if row is not None:
                self.stats[f"{source}:{fmt}"] += 1
                return row
            # An out-of-window id match must not hide an in-window alias
            # (label "4033" as one run's id and a later run's session_label).
        self.stats[f"unresolved:{fmt}"] += 1
        return None


def load_decisions_by_session(rows: Iterable[dict]) -> dict[str, dict]:
    """Index ``cascade-selector-decisions.jsonl`` rows by session id.

    The selector records one decision per run, keyed by the run uuid that is
    also the session record's ``session_id`` (``autonomous-run.sh
    --record-decision --session-id $SESSION_UUID``). That uuid is the decision
    id; no separate one exists. On duplicates the last row wins.
    """
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("session_id") or "").strip().lower()
        if sid:
            out[sid] = row
    return out


def dispatch_identity(row: dict, decisions: dict[str, dict] | None = None) -> dict:
    """What a session was dispatched to do, hoisted from where each field lives.

    ``reasoning_effort`` is top-level; task, pool, lane and experiment cell are
    nested in ``cascade_intent``; event dispatches carry ``dispatch_cause``
    instead of a selector decision. ``decision_id`` is the session id when
    ``decisions`` (from :func:`load_decisions_by_session`) holds a row for it,
    else ``None``.
    """
    intent = row.get("cascade_intent")
    intent = intent if isinstance(intent, dict) else {}
    cause = row.get("dispatch_cause")
    cause = cause if isinstance(cause, dict) else {}
    sid = str(row.get("session_id") or "").strip().lower()
    decision = (decisions or {}).get(sid) if sid else None
    return {
        "session_key": canonical_session_key(row),
        "decision_id": sid if decision is not None else None,
        "task_id": intent.get("task_id")
        or (decision or {}).get("execution_id")
        or cause.get("task"),
        "pool": intent.get("pool") or None,
        "lane": intent.get("cascade_lane") or None,
        "effort": row.get("reasoning_effort") or None,
        "experiment_cell": intent.get("experiment_cell") or None,
        "dispatch_kind": "selector"
        if decision is not None
        else (cause.get("event_type") or row.get("trigger") or None),
    }
