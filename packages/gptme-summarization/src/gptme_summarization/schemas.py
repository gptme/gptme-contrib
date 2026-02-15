"""
Schemas for recursive journal summarization.

Uses Chain-of-Key (CoK) pattern with structured JSON for incremental updates.
Based on research showing 40% accuracy improvement over baseline summarization.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

# Default repos for linkifying bare #NNN references.
# When text mentions "PR #123" or "#123" without a repo prefix, we can't know
# which repo it refers to — so we only linkify explicit "owner/repo#NNN" refs.
# The LLM prompt asks it to use full references like "gptme/gptme#1265".
_GITHUB_REF_RE = re.compile(r"(?<![/\w])([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#(\d+)")


def _linkify(text: str) -> str:
    """Turn owner/repo#NNN references into markdown links.

    Examples:
        "Fixed in gptme/gptme#1265" -> "Fixed in [gptme/gptme#1265](https://github.com/gptme/gptme/issues/1265)"
    """
    return _GITHUB_REF_RE.sub(r"[\1#\2](https://github.com/\1/issues/\2)", text)


class BlockerStatus(Enum):
    """Status of a blocker/issue."""

    ACTIVE = "active"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


@dataclass
class Decision:
    """A technical or strategic decision made during a session."""

    topic: str
    decision: str
    rationale: str
    session_id: Optional[str] = None


@dataclass
class Blocker:
    """An issue or blocker encountered."""

    issue: str
    status: BlockerStatus
    resolution: Optional[str] = None
    escalated_to: Optional[str] = None


@dataclass
class Metrics:
    """Quantitative metrics for a time period."""

    sessions: int = 0
    commits: int = 0
    prs_created: int = 0
    prs_merged: int = 0
    issues_created: int = 0
    issues_closed: int = 0
    tokens_used: int = 0
    # Session metadata from gptme logs
    models_used: list[str] = field(default_factory=list)
    total_tokens: int = 0
    total_cost: float = 0.0


def _format_metrics_line(metrics: "Metrics", sessions: int = 0) -> str:
    """Format a compact metrics line from a Metrics object."""
    parts = []
    if sessions:
        parts.append(f"**Sessions**: {sessions}")
    elif metrics.sessions:
        parts.append(f"**Sessions**: {metrics.sessions}")
    if metrics.commits:
        parts.append(f"**Commits**: {metrics.commits}")
    if metrics.prs_merged:
        parts.append(f"**PRs merged**: {metrics.prs_merged}")
    if metrics.issues_closed:
        parts.append(f"**Issues closed**: {metrics.issues_closed}")
    return " | ".join(parts) if parts else ""


def _format_session_metadata(metrics: "Metrics") -> str:
    """Format session metadata (models, tokens, cost) as a line."""
    parts = []
    if metrics.models_used:
        parts.append(f"**Models**: {', '.join(metrics.models_used)}")
    if metrics.total_tokens:
        if metrics.total_tokens >= 1_000_000:
            parts.append(f"**Tokens**: {metrics.total_tokens / 1_000_000:.1f}M")
        elif metrics.total_tokens >= 1_000:
            parts.append(f"**Tokens**: {metrics.total_tokens / 1_000:.1f}K")
        else:
            parts.append(f"**Tokens**: {metrics.total_tokens}")
    if metrics.total_cost > 0:
        parts.append(f"**Cost**: ${metrics.total_cost:.2f}")
    return " | ".join(parts) if parts else ""


@dataclass
class DailySummary:
    """
    Daily summary schema - summarizes all sessions from one day.

    Target: <500 tokens in rendered form.
    """

    date: date
    accomplishments: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    work_in_progress: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    narrative: str = ""
    key_insight: str = ""
    session_count: int = 0
    generated_at: Optional[datetime] = None

    def to_markdown(self) -> str:
        """Render as human-readable markdown."""
        lines = [
            f"# Daily Summary: {self.date.isoformat()}",
            "",
        ]

        metrics_line = _format_metrics_line(self.metrics, sessions=self.session_count)
        if metrics_line:
            lines.extend([metrics_line, ""])

        session_meta = _format_session_metadata(self.metrics)
        if session_meta:
            lines.extend([session_meta, ""])

        if self.narrative:
            lines.extend([self.narrative, ""])

        if self.accomplishments:
            lines.extend(
                [
                    "## Key Accomplishments",
                    *[f"- {a}" for a in self.accomplishments],
                    "",
                ]
            )

        if self.decisions:
            lines.extend(
                [
                    "## Decisions Made",
                    *[f"- **{d.topic}**: {d.decision}" for d in self.decisions],
                    "",
                ]
            )

        if self.blockers:
            active = [b for b in self.blockers if b.status == BlockerStatus.ACTIVE]
            resolved = [b for b in self.blockers if b.status == BlockerStatus.RESOLVED]
            if active:
                lines.extend(
                    [
                        "## Active Blockers",
                        *[f"- {b.issue}" for b in active],
                        "",
                    ]
                )
            if resolved:
                lines.extend(
                    [
                        "## Resolved",
                        *[f"- {b.issue}" for b in resolved],
                        "",
                    ]
                )

        if self.work_in_progress:
            lines.extend(
                [
                    "## Work in Progress",
                    *[f"- {w}" for w in self.work_in_progress],
                    "",
                ]
            )

        if self.key_insight:
            lines.extend(
                [
                    "## Key Insight",
                    self.key_insight,
                    "",
                ]
            )

        if self.themes:
            lines.extend(
                [
                    "## Themes",
                    f"*{', '.join(self.themes)}*",
                    "",
                ]
            )

        return _linkify("\n".join(lines))


@dataclass
class WeeklySummary:
    """
    Weekly summary schema - summarizes daily summaries for a week.

    Target: <1000 tokens in rendered form.
    """

    week: str  # ISO week format: YYYY-Www (e.g., "2025-W52")
    start_date: date
    end_date: date
    milestones: list[str] = field(default_factory=list)
    recurring_themes: list[str] = field(default_factory=list)
    key_decisions: list[Decision] = field(default_factory=list)
    progress_on_tasks: dict[str, str] = field(default_factory=dict)  # task_id -> progress summary
    blockers_summary: str = ""
    metrics: Metrics = field(default_factory=Metrics)
    narrative: str = ""
    trends: list[str] = field(default_factory=list)  # Observed patterns
    generated_at: Optional[datetime] = None

    def to_markdown(self) -> str:
        """Render as human-readable markdown."""
        lines = [
            f"# Weekly Summary: {self.week}",
            f"*{self.start_date.isoformat()} to {self.end_date.isoformat()}*",
            "",
        ]

        metrics_line = _format_metrics_line(self.metrics)
        if metrics_line:
            lines.extend([metrics_line, ""])

        session_meta = _format_session_metadata(self.metrics)
        if session_meta:
            lines.extend([session_meta, ""])

        if self.narrative:
            lines.extend([self.narrative, ""])

        if self.milestones:
            lines.extend(
                [
                    "## Major Milestones",
                    *[f"- {m}" for m in self.milestones],
                    "",
                ]
            )

        if self.progress_on_tasks:
            lines.extend(
                [
                    "## Task Progress",
                    *[
                        f"- **{task}**: {progress}"
                        for task, progress in self.progress_on_tasks.items()
                    ],
                    "",
                ]
            )

        if self.key_decisions:
            lines.extend(
                [
                    "## Key Decisions",
                    *[f"- **{d.topic}**: {d.decision}" for d in self.key_decisions],
                    "",
                ]
            )

        if self.recurring_themes:
            lines.extend(
                [
                    "## Recurring Themes",
                    f"*{', '.join(self.recurring_themes)}*",
                    "",
                ]
            )

        if self.trends:
            lines.extend(
                [
                    "## Observed Trends",
                    *[f"- {t}" for t in self.trends],
                    "",
                ]
            )

        if self.blockers_summary:
            lines.extend(
                [
                    "## Blockers Summary",
                    self.blockers_summary,
                    "",
                ]
            )

        return _linkify("\n".join(lines))


@dataclass
class MonthlySummary:
    """
    Monthly summary schema - summarizes weekly summaries for a month.

    Target: <2000 tokens in rendered form.
    """

    month: str  # Format: YYYY-MM (e.g., "2025-12")
    accomplishments: list[str] = field(default_factory=list)  # Major achievements
    goals_progress: dict[str, str] = field(default_factory=dict)  # goal -> status
    key_learnings: list[str] = field(default_factory=list)
    strategic_decisions: list[Decision] = field(default_factory=list)
    direction_for_next_month: list[str] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    month_narrative: str = ""
    highlights: list[str] = field(default_factory=list)  # Notable moments
    generated_at: Optional[datetime] = None

    def to_markdown(self) -> str:
        """Render as human-readable markdown."""
        lines = [
            f"# Monthly Summary: {self.month}",
            "",
        ]

        # Compact metrics block
        metrics_parts = []
        if self.metrics.sessions:
            metrics_parts.append(f"**Sessions**: {self.metrics.sessions}")
        if self.metrics.commits:
            metrics_parts.append(f"**Commits**: {self.metrics.commits}")
        if self.metrics.prs_merged:
            metrics_parts.append(f"**PRs merged**: {self.metrics.prs_merged}")
        if self.metrics.issues_closed:
            metrics_parts.append(f"**Issues closed**: {self.metrics.issues_closed}")
        if metrics_parts:
            lines.extend([" | ".join(metrics_parts), ""])

        session_meta = _format_session_metadata(self.metrics)
        if session_meta:
            lines.extend([session_meta, ""])

        if self.month_narrative:
            lines.extend([self.month_narrative, ""])

        if self.accomplishments:
            lines.extend(
                [
                    "## Major Accomplishments",
                    *[f"- {a}" for a in self.accomplishments],
                    "",
                ]
            )

        if self.goals_progress:
            lines.extend(
                [
                    "## Goals Progress",
                    *[f"- **{goal}**: {status}" for goal, status in self.goals_progress.items()],
                    "",
                ]
            )

        if self.key_learnings:
            lines.extend(
                [
                    "## Key Learnings",
                    *[f"- {learning}" for learning in self.key_learnings],
                    "",
                ]
            )

        if self.strategic_decisions:
            lines.extend(
                [
                    "## Strategic Decisions",
                    *[f"- **{d.topic}**: {d.decision}" for d in self.strategic_decisions],
                    "",
                ]
            )

        if self.highlights:
            lines.extend(
                [
                    "## Highlights",
                    *[f"- {h}" for h in self.highlights],
                    "",
                ]
            )

        if self.direction_for_next_month:
            lines.extend(
                [
                    "## Direction for Next Month",
                    *[f"- {d}" for d in self.direction_for_next_month],
                    "",
                ]
            )

        return _linkify("\n".join(lines))
