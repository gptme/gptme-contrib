"""
Schemas for recursive journal summarization.

Uses Chain-of-Key (CoK) pattern with structured JSON for incremental updates.
Based on research showing 40% accuracy improvement over baseline summarization.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


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
    session_count: int = 0
    generated_at: Optional[datetime] = None

    def to_markdown(self) -> str:
        """Render as human-readable markdown."""
        lines = [
            f"# Daily Summary: {self.date.isoformat()}",
            "",
            f"**Sessions**: {self.session_count} | **Commits**: {self.metrics.commits} | **PRs**: {self.metrics.prs_merged} merged",
            "",
        ]

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
                        *[f"- ⚠️ {b.issue}" for b in active],
                        "",
                    ]
                )
            if resolved:
                lines.extend(
                    [
                        "## Resolved Blockers",
                        *[f"- ✅ {b.issue}" for b in resolved],
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

        if self.themes:
            lines.extend(
                [
                    "## Themes",
                    f"*{', '.join(self.themes)}*",
                    "",
                ]
            )

        return "\n".join(lines)


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
    trends: list[str] = field(default_factory=list)  # Observed patterns
    generated_at: Optional[datetime] = None

    def to_markdown(self) -> str:
        """Render as human-readable markdown."""
        lines = [
            f"# Weekly Summary: {self.week}",
            f"*{self.start_date.isoformat()} to {self.end_date.isoformat()}*",
            "",
            f"**Sessions**: {self.metrics.sessions} | **Commits**: {self.metrics.commits} | **PRs Merged**: {self.metrics.prs_merged}",
            "",
        ]

        if self.milestones:
            lines.extend(
                [
                    "## Major Milestones",
                    *[f"- 🎯 {m}" for m in self.milestones],
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

        return "\n".join(lines)


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
    highlights: list[str] = field(default_factory=list)  # Notable moments
    generated_at: Optional[datetime] = None

    def to_markdown(self) -> str:
        """Render as human-readable markdown."""
        lines = [
            f"# Monthly Summary: {self.month}",
            "",
            "## Metrics",
            f"- **Sessions**: {self.metrics.sessions}",
            f"- **Commits**: {self.metrics.commits}",
            f"- **PRs Merged**: {self.metrics.prs_merged}",
            f"- **Issues Closed**: {self.metrics.issues_closed}",
            "",
        ]

        if self.accomplishments:
            lines.extend(
                [
                    "## Major Accomplishments",
                    *[f"- 🏆 {a}" for a in self.accomplishments],
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
                    *[f"- 📚 {learning}" for learning in self.key_learnings],
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
                    *[f"- ✨ {h}" for h in self.highlights],
                    "",
                ]
            )

        if self.direction_for_next_month:
            lines.extend(
                [
                    "## Direction for Next Month",
                    *[f"- → {d}" for d in self.direction_for_next_month],
                    "",
                ]
            )

        return "\n".join(lines)
