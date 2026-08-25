"""Injection logging and index-health (rot) reporting for gptme-rag.

Two failure modes make a retrieval system quietly useless, and neither is
visible from inside the retrieval code:

1. **Invisible injections.** Without a per-injection record there is no way to
   ask whether retrieval fired, what it put in front of the model, or whether
   the hits correlated with anything downstream. Tuning a floor without this is
   guesswork.
2. **Silent rot.** A stale index keeps answering queries — with stale documents
   — and nothing fails. Bob's ambient injector ran on a dead index for 51 days
   before anyone noticed, because "no error" and "working" look identical from
   the consumer side.

Both are deliberately *policy-free*. This module never decides which
directories are sources, how documents are counted, or what an acceptable
staleness is; callers pass counts and thresholds. That split is what keeps the
consumer's configuration out of the library.

Ported from the Bob brain script (``scripts/build-ambient-memory-index.py``)
where both patterns were measured against real session data.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

#: Process names, as seen in ``/proc/<pid>/comm``, mapped to a harness label.
#: Extend this rather than branching at the call site.
HARNESS_COMMS: dict[str, str] = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "gptme": "gptme",
    "codex": "codex",
    "codex-cli": "codex",
    "copilot": "copilot-cli",
    "copilot-cli": "copilot-cli",
    "gh-copilot": "copilot-cli",
}

#: Environment variables consulted, in order, to identify the calling session.
DEFAULT_SESSION_ENV_VARS: tuple[str, ...] = (
    "GPTME_RAG_SESSION_ID",
    "GPTME_SESSION_ID",
    "CC_SESSION_ID",
)

#: How far up the process tree :func:`detect_harness` will walk.
_MAX_PROC_WALK = 8

#: Worse status wins. A missing slice must not be masked by a stale global
#: count the way a missing slice used to be masked by a healthy one.
_STATUS_RANK = {"ok": 0, "stale": 1, "missing": 2}


def _worse_status(current: str, candidate: str) -> str:
    """Return the more severe of two health statuses."""
    if _STATUS_RANK.get(candidate, 0) > _STATUS_RANK.get(current, 0):
        return candidate
    return current


def detect_harness() -> str:
    """Best-effort identification of the agent harness that invoked us.

    Walks the process tree looking for a known harness process name. Returns
    ``"unknown"`` when nothing matches or ``/proc`` is unavailable — this is
    diagnostic metadata, never a control-flow input.
    """
    pid = os.getppid()
    for _ in range(_MAX_PROC_WALK):
        if pid <= 1:
            break
        try:
            comm = (Path("/proc") / str(pid) / "comm").read_text().strip()
        except OSError:
            break
        if comm in HARNESS_COMMS:
            return HARNESS_COMMS[comm]
        try:
            status = (Path("/proc") / str(pid) / "status").read_text()
        except OSError:
            break
        ppid = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                try:
                    ppid = int(line.split()[1])
                except (ValueError, IndexError):
                    ppid = 0
                break
        if ppid == pid or ppid <= 1:
            break
        pid = ppid
    return "unknown"


def detect_session_id(env_vars: Sequence[str] = DEFAULT_SESSION_ENV_VARS) -> str:
    """Return the first non-empty session id from ``env_vars``, else ``unknown``."""
    for name in env_vars:
        value = os.environ.get(name)
        if value:
            return value
    return "unknown"


def _as_score(value: Any) -> float:
    """Coerce a hit score to float; unusable values become 0.0 rather than raise."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _copyable_hit_field(value: Any) -> bool:
    """Keep present values including 0/False; drop None and empty strings."""
    return value is not None and value != ""


def log_injection(
    log_file: Path,
    query: str,
    hits: Sequence[Mapping[str, Any]],
    *,
    backend: str,
    max_injected: int | None = None,
    session_id: str | None = None,
    harness: str | None = None,
    score_key: str = "similarity",
    hit_fields: Sequence[str] = ("id", "type", "path", "date", "state"),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append one JSONL record describing a retrieval injection.

    Args:
        log_file: JSONL file to append to; parent directories are created.
        query: The query text that produced ``hits``.
        hits: Ranked hits, richest first. Only the first ``max_injected`` are
            recorded in detail — the rest are still counted.
        backend: Retrieval backend label (``"tfidf"``, ``"chroma"``, ...).
        max_injected: How many hits the consumer actually injected. Defaults to
            all of them.
        session_id: Overrides :func:`detect_session_id`.
        harness: Overrides :func:`detect_harness`.
        score_key: Key holding each hit's score.
        hit_fields: Hit keys copied into the record when present. ``None`` and
            ``""`` are omitted; ``0`` / ``False`` are kept.
        extra: Additional top-level fields merged into the record.

    Returns the record written, or ``None`` if the write failed. Logging is
    best-effort by design: a full disk must never break injection.
    """
    injected = len(hits) if max_injected is None else max(0, min(len(hits), max_injected))
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": session_id if session_id is not None else detect_session_id(),
        "harness": harness if harness is not None else detect_harness(),
        "backend": backend,
        "query": query,
        "query_len": len(query),
        "num_hits": len(hits),
        "num_injected": injected,
        "top_score": _as_score(hits[0].get(score_key)) if hits else 0.0,
        "hits": [
            {
                **{f: hit[f] for f in hit_fields if f in hit and _copyable_hit_field(hit[f])},
                "score": _as_score(hit.get(score_key)),
            }
            for hit in hits[:injected]
        ],
    }
    if extra:
        record.update(extra)

    try:
        payload = json.dumps(record) + "\n"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(payload)
    except (OSError, TypeError, ValueError) as exc:
        # Best-effort: a full disk or an unserializable hit must never break injection.
        logger.debug("injection log write failed (%s): %s", log_file, exc)
        return None
    return record


@dataclass
class InjectionStats:
    """Aggregate view of an injection log."""

    total: int = 0
    nonzero: int = 0
    avg_hits: float = 0.0
    avg_top_score: float = 0.0
    unique_sessions: int = 0
    by_harness: dict[str, int] = field(default_factory=dict)
    malformed: int = 0

    @property
    def fire_rate(self) -> float:
        """Fraction of injections that returned at least one hit."""
        return self.nonzero / self.total if self.total else 0.0


def summarize_injections(log_file: Path) -> InjectionStats:
    """Aggregate an injection log into :class:`InjectionStats`.

    A concurrently-appended JSONL file routinely ends in a partially-written
    line, so undecodable lines are counted in ``malformed`` and skipped rather
    than raising.
    """
    stats = InjectionStats()
    if not log_file.exists():
        return stats

    sessions: set[str] = set()
    sum_hits = 0
    sum_top = 0.0
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats.malformed += 1
                continue
            if not isinstance(rec, dict):
                stats.malformed += 1
                continue
            try:
                num_hits = int(rec.get("num_hits", 0) or 0)
                top_score = float(rec.get("top_score", 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                # Same contract as a partial trailing line: skip, don't raise.
                stats.malformed += 1
                continue
            stats.total += 1
            sum_hits += num_hits
            if num_hits > 0:
                stats.nonzero += 1
                sum_top += top_score
            sessions.add(str(rec.get("session_id") or "unknown"))
            harness = str(rec.get("harness") or "unknown")
            stats.by_harness[harness] = stats.by_harness.get(harness, 0) + 1

    stats.unique_sessions = len(sessions)
    if stats.total:
        stats.avg_hits = sum_hits / stats.total
    if stats.nonzero:
        stats.avg_top_score = sum_top / stats.nonzero
    return stats


@dataclass
class SliceHealth:
    """Health of one named subset of the index (a document type, a source)."""

    name: str
    indexed: int
    on_disk: int
    status: str

    @property
    def delta(self) -> int:
        return self.on_disk - self.indexed


@dataclass
class IndexHealth:
    """Whether an index is fresh enough and complete enough to be trusted."""

    status: str
    age_hours: float | None = None
    built_at: str | None = None
    indexed_count: int = 0
    on_disk_count: int | None = None
    slices: dict[str, SliceHealth] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def delta(self) -> int | None:
        if self.on_disk_count is None:
            return None
        return self.on_disk_count - self.indexed_count

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "age_hours": self.age_hours,
            "built_at": self.built_at,
            "indexed_count": self.indexed_count,
            "on_disk_count": self.on_disk_count,
            "delta": self.delta,
            "reasons": list(self.reasons),
        }
        if self.slices:
            data["slices"] = {
                name: {
                    "status": s.status,
                    "indexed": s.indexed,
                    "on_disk": s.on_disk,
                    "delta": s.delta,
                }
                for name, s in self.slices.items()
            }
        return data


def assess_index_health(
    *,
    built_at: datetime | None,
    indexed_count: int,
    on_disk_count: int | None = None,
    max_age_hours: float = 2.0,
    count_delta_tolerance: int = 0,
    slices: Mapping[str, tuple[int, int]] | None = None,
    slice_delta_tolerance: int = 25,
    now: datetime | None = None,
) -> IndexHealth:
    """Judge index freshness and completeness from counts the caller supplies.

    Args:
        built_at: When the index was built; ``None`` means no index exists.
        indexed_count: Documents in the index.
        on_disk_count: Documents currently on disk, if the caller can count
            them cheaply. ``None`` skips the completeness check.
        max_age_hours: Above this age the index is ``stale``.
        count_delta_tolerance: Absolute indexed/on-disk difference tolerated
            before the index is ``stale``. A continuously-written corpus should
            allow some churn; a static one should use ``0``.
        slices: ``{name: (indexed, on_disk)}`` for subsets worth judging
            separately. A slice at zero is the rot that matters most: global
            counts stay healthy while one document type has silently vanished
            from the index.
        slice_delta_tolerance: Same as ``count_delta_tolerance``, per slice.
        now: Injectable clock for tests.

    Returns an :class:`IndexHealth` whose ``status`` is one of ``ok``,
    ``stale`` or ``missing``, with ``reasons`` naming every failed check.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if built_at is None:
        return IndexHealth(status="missing", reasons=["no index"])

    if built_at.tzinfo is None:
        built_at = built_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - built_at).total_seconds() / 3600.0)

    health = IndexHealth(
        status="ok",
        age_hours=round(age_hours, 2),
        built_at=built_at.isoformat(),
        indexed_count=indexed_count,
        on_disk_count=on_disk_count,
    )

    if indexed_count == 0:
        health.status = "missing"
        health.reasons.append("index is empty")
        return health

    if age_hours > max_age_hours:
        health.status = "stale"
        health.reasons.append(f"age {age_hours:.1f}h > {max_age_hours}h")

    if on_disk_count is not None:
        delta = on_disk_count - indexed_count
        if abs(delta) > count_delta_tolerance:
            health.status = "stale"
            health.reasons.append(f"count delta {delta:+d} > tolerance {count_delta_tolerance}")

    for name, (indexed, on_disk) in (slices or {}).items():
        delta = on_disk - indexed
        if indexed == 0:
            slice_status = "missing"
            health.reasons.append(f"slice {name!r} is empty")
        elif abs(delta) > slice_delta_tolerance or age_hours > max_age_hours:
            slice_status = "stale"
            if abs(delta) > slice_delta_tolerance:
                health.reasons.append(
                    f"slice {name!r} delta {delta:+d} > tolerance {slice_delta_tolerance}"
                )
            else:
                health.reasons.append(
                    f"slice {name!r} stale (age {age_hours:.1f}h > {max_age_hours}h)"
                )
        else:
            slice_status = "ok"
        health.slices[name] = SliceHealth(
            name=name, indexed=indexed, on_disk=on_disk, status=slice_status
        )
        # A dead slice must not be masked by a healthier global status --
        # missing-while-stale is the same rot as missing-while-ok.
        health.status = _worse_status(health.status, slice_status)

    return health


__all__ = [
    "HARNESS_COMMS",
    "DEFAULT_SESSION_ENV_VARS",
    "IndexHealth",
    "InjectionStats",
    "SliceHealth",
    "assess_index_health",
    "detect_harness",
    "detect_session_id",
    "log_injection",
    "summarize_injections",
]
