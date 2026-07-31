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
    return dt


def _extract_outcome_line(path: Path) -> str | None:
    """Return the outcome summary from a journal file, or None."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("**Outcome**:"):
                continue
            summary = line.split(":", 1)[1].strip()
            # Strip "productive — " or "blocked — " prefix
            if "—" in summary:
                _, rest = summary.split("—", 1)
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


def get_standup_context(
    journal_dir: Path,
    since: datetime,
    *,
    limit: int = 8,
    include_low_signal: bool = False,
) -> StandupContext:
    """Extract journal outcome summaries since `since`.

    Scans journal/YYYY-MM-DD/*.md files whose mtime is after `since`,
    extracts **Outcome** lines, filters low-signal entries unless
    include_low_signal=True, and returns up to `limit` summaries ordered
    newest-first.
    """
    ctx = StandupContext(since=since)

    if not journal_dir.exists():
        return ctx

    candidates: list[tuple[float, Path]] = []

    # Scan only YYYY-MM-DD date directories whose date is >= since (UTC date).
    # Non-date entries (e.g. 'templates/') are skipped.
    # Filtering by directory date (not file mtime) is semantically correct:
    # the directory name IS the journal date regardless of when the file was written.
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

        for path in date_dir.glob("*.md"):
            if path.name == "self-merges.md":
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((mtime, path))

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
