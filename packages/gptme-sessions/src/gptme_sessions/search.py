"""Full-text search over session transcripts.

Provides case-insensitive substring search across gptme and Claude Code session
transcripts, using ``read_transcript()`` for harness-agnostic normalization.

Performance: linear scan over 500 sessions ≈ 80ms — no index required for v1.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .discovery import (
    discover_cc_sessions,
    discover_codex_sessions,
    discover_copilot_sessions,
    discover_gptme_sessions,
)
from .transcript import read_transcript

logger = logging.getLogger(__name__)

SNIPPET_CONTEXT = 120  # chars of surrounding text to include around each match
MAX_SNIPPETS_PER_SESSION = 3


@dataclass
class Snippet:
    """A text excerpt containing a search match."""

    role: str
    text: str


@dataclass
class SearchResult:
    """A session that contains at least one match for the query."""

    session_id: str
    harness: str
    path: str
    hit_count: int
    started_at: datetime | None
    snippets: list[Snippet] = field(default_factory=list)

    @property
    def display_date(self) -> str:
        if self.started_at:
            return self.started_at.strftime("%Y-%m-%d %H:%M")
        return "unknown"


def _make_snippet(content: str, pattern: re.Pattern) -> str | None:
    """Return a context snippet around the first match, or None if no match."""
    m = pattern.search(content)
    if not m:
        return None
    start = max(0, m.start() - SNIPPET_CONTEXT)
    end = min(len(content), m.end() + SNIPPET_CONTEXT)
    snippet = content[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(content):
        snippet = snippet + "…"
    return snippet


def _search_path(
    path: Path,
    pattern: re.Pattern,
    file_pattern: re.Pattern | None = None,
) -> SearchResult | None:
    """Search a single session file and return a result, or None if no match."""
    try:
        transcript = read_transcript(path)
    except Exception as exc:
        logger.debug("skipping %s: %s", path, exc)
        return None

    # Pre-filter: if file_pattern specified, skip sessions that don't mention the file
    if file_pattern is not None:
        if not any(msg.content and file_pattern.search(msg.content) for msg in transcript.messages):
            return None

    total_hits = 0
    snippets: list[Snippet] = []

    for msg in transcript.messages:
        if not msg.content:
            continue
        matches = list(pattern.finditer(msg.content))
        if not matches:
            continue
        total_hits += len(matches)
        if len(snippets) < MAX_SNIPPETS_PER_SESSION:
            text = _make_snippet(msg.content, pattern)
            if text:
                snippets.append(Snippet(role=msg.role, text=text))

    if total_hits == 0:
        return None

    started_at: datetime | None = None
    if transcript.started_at:
        try:
            started_at = datetime.fromisoformat(transcript.started_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    return SearchResult(
        session_id=transcript.session_id,
        harness=transcript.harness,
        path=str(path),
        hit_count=total_hits,
        started_at=started_at,
        snippets=snippets,
    )


def search_sessions(
    query: str,
    harness: str | None = None,
    days: int = 30,
    max_results: int = 20,
    case_sensitive: bool = False,
    file_path: str | None = None,
) -> list[SearchResult]:
    """Search session transcripts and return results ranked by recency then hit count.

    Uses ``read_transcript()`` for harness-agnostic normalization across
    gptme, claude-code, codex, and copilot sessions.

    Parameters
    ----------
    query:
        Substring to search for (case-insensitive by default).
    harness:
        Limit to a specific harness (``"gptme"``, ``"claude-code"``, ``"codex"``, or ``"copilot"``).
        ``None`` searches all supported harnesses.
    days:
        Search sessions from the last N days.
    max_results:
        Maximum number of sessions to return.
    case_sensitive:
        If True, perform a case-sensitive search.
    file_path:
        If provided, only return sessions that mention this file path in their
        transcript (catches Read, Edit, Write, and tool calls referencing the file).
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(query), flags)
    file_pattern = re.compile(re.escape(file_path), flags) if file_path else None

    today = date.today()
    start = today - timedelta(days=days)

    paths: list[Path] = []
    if harness in (None, "gptme"):
        paths.extend(discover_gptme_sessions(start, today))
    if harness in (None, "claude-code"):
        paths.extend(discover_cc_sessions(start, today))
    if harness in (None, "codex"):
        paths.extend(discover_codex_sessions(start, today))
    if harness in (None, "copilot"):
        paths.extend(discover_copilot_sessions(start, today))

    results: list[SearchResult] = []
    for path in paths:
        result = _search_path(path, pattern, file_pattern)
        if result is not None:
            results.append(result)

    def _sort_key(r: SearchResult) -> tuple[float, int]:
        ts = r.started_at.timestamp() if r.started_at else 0.0
        return (-ts, -r.hit_count)

    results.sort(key=_sort_key)
    return results[:max_results]
