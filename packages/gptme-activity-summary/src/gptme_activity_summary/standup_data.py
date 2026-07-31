"""
Standup context extraction from journal outcome summaries.

Provides a reusable "since-last-standup" digest that voice handlers and
other callers can inject as pre-fetched context instead of using raw commit
subjects (which are implementation-biased and incomplete).

See: ErikBjare/bob#918, gptme-contrib#1333
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gptme_sessions.store import SessionStore


# Phrases whose presence marks a progress entry as internal bookkeeping
# rather than meaningful standup content (keep narrow — overfitting hurts).
_LOW_SIGNAL_PATTERNS = (
    r"\btypecheck\b",
    r"\blint\b",
    r"\bloo analysis\b",
    r"\bself-review\b",
    r"\bstate saved\b",
    r"\bgreptile findings tracked\b",
    r"\bpre-commit hooks?\b",
    r"\bno new lessons?\b",
)
_OUTCOME_STATUS_PREFIXES = (
    "blocked",
    "healthy",
    "no-action",
    "no-op",
    "noop",
    "noop-soft",
    "partial",
    "productive",
    "productive (minor)",
    "productive (restraint)",
    "productive restraint",
    "productive-minor",
    "restraint",
    "restrained",
)


def parse_since(since: str) -> datetime:
    """Parse a --since value into an aware UTC datetime.

    Accepts:
      "24h", "48h", "Nh"  — N hours ago
      "1d", "3d"          — N days ago
      ISO 8601 string     — parsed as UTC if no tz info
    """
    since = since.strip()
    now = datetime.now(timezone.utc)

    # Duration: "24h", "48h", "2h"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)h", since, re.IGNORECASE)
    if m:
        return now - timedelta(hours=float(m.group(1)))

    # Duration: "1d", "3d"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)d", since, re.IGNORECASE)
    if m:
        return now - timedelta(days=float(m.group(1)))

    # ISO timestamp
    dt = datetime.fromisoformat(since.rstrip("Z").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _extract_outcome_line(path: Path) -> str | None:
    """Return the outcome summary from a journal file, or None."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("**Outcome**:"):
                continue
            summary = line.split(":", 1)[1].strip()
            # Strip conventional journal status prefixes, preserving em dashes
            # that are part of free-form outcome prose.
            prefix, separator, rest = summary.partition(" — ")
            if separator and prefix.casefold() in _OUTCOME_STATUS_PREFIXES:
                summary = rest.strip()
            return summary or None
    except OSError:
        return None
    return None


def _is_low_signal(summary: str) -> bool:
    normalized = summary.strip().lower()
    return any(re.search(p, normalized) for p in _LOW_SIGNAL_PATTERNS)


@dataclass
class JournalSummary:
    date: str  # YYYY-MM-DD
    session: str  # journal filename stem
    summary: str
    low_signal: bool = False


@dataclass
class StandupContext:
    since: datetime
    journal_summaries: list[JournalSummary] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "since": self.since.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "journal_summaries": [
                {
                    "date": s.date,
                    "session": s.session,
                    "summary": s.summary,
                    "low_signal": s.low_signal,
                }
                for s in self.journal_summaries
            ],
        }


def _journal_completion_times(journal_dir: Path) -> dict[Path, datetime]:
    """Return immutable session completion times keyed by journal path."""
    workspace = journal_dir.parent.resolve()
    completion_times: dict[Path, datetime] = {}

    for record in SessionStore().load_all():
        if not record.journal_path:
            continue
        completed_at = record.end_time or record.timestamp
        if not completed_at:
            continue
        try:
            completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)

        path = Path(record.journal_path)
        if not path.is_absolute():
            path = workspace / path
        path = path.resolve()
        completed = completed.astimezone(timezone.utc)
        previous = completion_times.get(path)
        if previous is None or completed > previous:
            completion_times[path] = completed

    return completion_times


def get_standup_context(
    journal_dir: Path,
    since: datetime,
    *,
    limit: int = 8,
    include_low_signal: bool = False,
) -> StandupContext:
    """Extract journal outcome summaries since `since`.

    Scans journal/YYYY-MM-DD/*.md files whose recorded session completion time
    is after `since`, filters low-signal entries unless include_low_signal=True,
    and returns up to `limit` summaries ordered newest-first. Files without a
    session record use their journal date at midnight UTC, preserving whole-day
    lookbacks without treating mutable filesystem metadata as occurrence time.
    """
    ctx = StandupContext(since=since)

    if not journal_dir.exists():
        return ctx

    candidates: list[tuple[datetime, Path]] = []
    since = since.astimezone(timezone.utc)
    completion_times = _journal_completion_times(journal_dir)

    # Scan only YYYY-MM-DD date directories whose date is >= since (UTC date).
    # Non-date entries (e.g. 'templates/') are skipped.
    since_date_str = since.date().isoformat()

    date_dirs = sorted(
        (
            d
            for d in journal_dir.iterdir()
            if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)
        ),
        reverse=True,
    )

    for date_dir in date_dirs:
        if date_dir.name < since_date_str:
            break  # all remaining dirs are older; sorted so safe to stop

        fallback_time = datetime.fromisoformat(date_dir.name).replace(tzinfo=timezone.utc)
        for path in date_dir.glob("*.md"):
            if path.name == "self-merges.md":
                continue
            completed = completion_times.get(path.resolve(), fallback_time)
            if completed >= since:
                candidates.append((completed, path))

    # Newest first
    candidates.sort(key=lambda t: t[0], reverse=True)

    high: list[JournalSummary] = []
    low: list[JournalSummary] = []

    for _mtime, path in candidates:
        outcome = _extract_outcome_line(path)
        if not outcome:
            continue
        entry = JournalSummary(
            date=path.parent.name,
            session=path.stem,
            summary=outcome,
            low_signal=_is_low_signal(outcome),
        )
        (low if entry.low_signal else high).append(entry)

    selected = high[:limit]
    if include_low_signal and len(selected) < limit:
        selected.extend(low[: limit - len(selected)])

    ctx.journal_summaries = selected
    return ctx
