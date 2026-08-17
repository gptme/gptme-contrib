"""Task-scoped retrieval for gptme-rag.

Retrieval over a general corpus silently drops tasks — they are ~5% of
documents and never reach global top-N.  This module provides a task-specific
ranking layer that operates on an index built from task files only, plus a
calibrated silence rule so the injector stays quiet when no task is relevant.

Ported from the Bob brain script (``scripts/build-ambient-memory-index.py``)
where the pattern was measured and tuned on real session data.

Requires ``gptme-rag[lexical]`` (scikit-learn).  Import raises
:class:`~gptme_rag.lexical.LexicalDependencyMissing` if scikit-learn is absent.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .indexing.document import Document
from .lexical import TfidfIndex

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (calibrated on real Bob session data — do not lower without
# measuring precision/recall against a representative query set)
# ---------------------------------------------------------------------------

#: Below this cosine-similarity floor the injector emits nothing at all.
#: Measured floor for useful task matches is ~0.28–0.31; 0.27 is the safe
#: lower bound that avoids injecting unrelated tasks on ambiguous prompts.
TASK_RELEVANCE_FLOOR: float = 0.27

#: Trailing hits weaker than top_score × this ratio are dropped.
#: Prevents one strong match from dragging in two much-weaker companions.
TASK_TRAILING_RATIO: float = 0.55

#: Default maximum task hits returned per query.
MAX_TASKS_PER_QUERY: int = 3

TASK_OPEN_STATES: frozenset[str] = frozenset(
    {"backlog", "todo", "active", "waiting", "ready_for_review", "someday", "new", "paused"}
)
TASK_CLOSED_STATES: frozenset[str] = frozenset({"done", "cancelled", "archived"})

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_YAML_KEY_RE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)


@dataclass
class TaskHit:
    """A ranked task retrieval result."""

    document: Document
    score: float
    title: str
    path: str
    state: str
    closed: bool = field(init=False)

    def __post_init__(self) -> None:
        self.closed = self.state in TASK_CLOSED_STATES


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract key–value pairs from a YAML-like frontmatter block.

    Only handles simple ``key: value`` pairs (no nested structures).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    return dict(_YAML_KEY_RE.findall(m.group(1)))


def _extract_title(text: str, frontmatter: dict[str, str], stem: str) -> str:
    """Derive a human-readable title from frontmatter > markdown heading > filename."""
    if "title" in frontmatter:
        return frontmatter["title"].strip().strip('"').strip("'")
    heading = _TITLE_RE.search(text)
    if heading:
        return heading.group(1).strip()
    return stem.replace("-", " ").replace("_", " ").title()


def load_task_documents(
    tasks_dir: Path,
    *,
    include_archived: bool = True,
) -> list[Document]:
    """Read task Markdown files and return a list of :class:`~gptme_rag.indexing.document.Document`.

    Each document gets task-specific metadata:

    * ``type``: ``"task"``
    * ``task_state``: value of the ``state:`` frontmatter field
    * ``task_archived``: whether the file lives under an ``archive/`` subdirectory
    * ``title``: human-readable task title

    Args:
        tasks_dir: Root directory containing task ``.md`` files.
        include_archived: When False, skip files under ``archive/`` subdirectories.

    Returns:
        List of :class:`~gptme_rag.indexing.document.Document` objects ready
        to be passed to :class:`~gptme_rag.lexical.TfidfIndex.index`.
    """
    tasks_dir = Path(tasks_dir)
    docs: list[Document] = []
    for path in sorted(tasks_dir.rglob("*.md")):
        rel = path.relative_to(tasks_dir)
        parts = rel.parts
        # Skip template files
        if any(part in {"templates", "__pycache__"} for part in parts):
            continue
        archived = "archive" in parts
        if archived and not include_archived:
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("task_retrieval: skipping unreadable file %s", path)
            continue

        fm = _parse_frontmatter(text)
        state_raw = fm.get("state", "").strip()
        state = state_raw if state_raw else ("archived" if archived else "unknown")
        title = _extract_title(text, fm, path.stem)

        metadata: dict[str, Any] = {
            "type": "task",
            "source": str(path),
            "title": title,
            "task_state": state,
            "task_archived": archived,
        }
        docs.append(
            Document(
                content=text,
                metadata=metadata,
                source_path=path,
            )
        )
    return docs


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def rank_tasks(
    index: TfidfIndex,
    query: str,
    n_results: int = MAX_TASKS_PER_QUERY,
    include_closed: bool = True,
    exclude_paths: set[str] | None = None,
) -> list[TaskHit]:
    """Rank task documents against *query* and return up to *n_results* hits.

    The *index* should be built from task documents only (via
    :func:`load_task_documents`).  Querying a mixed-document index returns
    poor results because tasks are outnumbered by other content.

    Args:
        index: A :class:`~gptme_rag.lexical.TfidfIndex` fitted on task documents.
        query: Free-text query (typically the session prompt or a topic string).
        n_results: Maximum hits to return before silence-rule filtering.
        include_closed: When False, omit done/cancelled/archived tasks.
        exclude_paths: Set of source paths to skip (e.g. already in context).

    Returns:
        Ranked list of :class:`TaskHit`.  May be longer than *n_results* if
        ``include_closed=False`` causes many filtered results.
    """
    if not query.strip():
        return []

    # Over-fetch so filtering (include_closed, exclude_paths) leaves enough hits.
    raw_hits = index.search(
        query,
        n_results=max(n_results * 4, 20),
        exclude_paths=exclude_paths,
    )

    hits: list[TaskHit] = []
    for h in raw_hits:
        doc = h.document
        state = str(doc.metadata.get("task_state") or "unknown")
        if not include_closed and state in TASK_CLOSED_STATES:
            continue
        title = str(
            doc.metadata.get("title")
            or (doc.source_path.stem if doc.source_path else "")
            or "Unknown"
        )
        path = str(doc.source_path or doc.metadata.get("source", ""))
        hits.append(
            TaskHit(
                document=doc,
                score=h.score,
                title=title,
                path=path,
                state=state,
            )
        )
        if len(hits) >= n_results:
            break

    return hits


# ---------------------------------------------------------------------------
# Silence rule
# ---------------------------------------------------------------------------


def apply_task_silence_rule(
    hits: list[TaskHit],
    floor: float = TASK_RELEVANCE_FLOOR,
    trailing_ratio: float = TASK_TRAILING_RATIO,
) -> list[TaskHit]:
    """Drop hits below the relevance floor and trailing weak hits.

    The silence rule keeps the injector quiet on ~73% of real prompts.
    It has two clauses:

    1. **Floor**: if the top hit is below *floor*, return ``[]`` — the whole
       candidate set is too weak to inject.
    2. **Trailing ratio**: hits weaker than ``top_score × trailing_ratio`` are
       dropped so a single strong match does not drag in weak companions.

    Args:
        hits: Ranked list from :func:`rank_tasks`.
        floor: Minimum cosine similarity for the top hit to permit any injection.
        trailing_ratio: Minimum ``score / top_score`` ratio for a hit to survive.

    Returns:
        Filtered list, possibly empty.
    """
    if not hits:
        return []
    top_score = hits[0].score
    if top_score < floor:
        return []
    return [h for h in hits if h.score >= floor and h.score >= top_score * trailing_ratio]


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def query_tasks(
    index: TfidfIndex,
    query: str,
    n_results: int = MAX_TASKS_PER_QUERY,
    floor: float = TASK_RELEVANCE_FLOOR,
    include_closed: bool = True,
    exclude_paths: set[str] | None = None,
) -> list[TaskHit]:
    """Return task hits above the relevance floor (empty → inject nothing).

    Convenience wrapper that combines :func:`rank_tasks` and
    :func:`apply_task_silence_rule`.

    Args:
        index: TF-IDF index fitted on task documents.
        query: Free-text query.
        n_results: Maximum returned hits (before silence-rule filtering).
        floor: Silence-rule relevance floor.
        include_closed: When False, omit done/cancelled/archived tasks.
        exclude_paths: Paths to skip in results.

    Returns:
        Ranked, filtered list of :class:`TaskHit`.
    """
    hits = rank_tasks(
        index,
        query,
        n_results=n_results,
        include_closed=include_closed,
        exclude_paths=exclude_paths,
    )
    return apply_task_silence_rule(hits, floor=floor)


# ---------------------------------------------------------------------------
# Injection formatting
# ---------------------------------------------------------------------------


def format_task_injection(hits: list[TaskHit]) -> str:
    """Format task hits as a compact pre-session injection block.

    Short by design: the point is that a session *notices* an existing lane
    before starting new work, not that it reads the full task inline.

    Returns an empty string when *hits* is empty.
    """
    if not hits:
        return ""

    lines = [
        "## Possibly-Related Existing Tasks (retrieved, not selected)",
        "",
        "*Matched against your prompt. Check before starting new work — one of "
        "these may already own it, or may record that it was already tried.*",
        "",
    ]
    for hit in hits:
        marker = "already attempted" if hit.closed else "open lane"
        lines.append(
            f"- **{hit.title}** — `{hit.state}` ({marker}, "
            f"similarity {hit.score:.2f})  \n"
            f"  `{hit.path}`"
        )
    lines.extend(
        [
            "",
            "*If one of these covers your work item, read it before duplicating it. "
            "If none do, ignore this block.*",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "TASK_RELEVANCE_FLOOR",
    "TASK_TRAILING_RATIO",
    "MAX_TASKS_PER_QUERY",
    "TASK_OPEN_STATES",
    "TASK_CLOSED_STATES",
    "TaskHit",
    "load_task_documents",
    "rank_tasks",
    "apply_task_silence_rule",
    "query_tasks",
    "format_task_injection",
]
