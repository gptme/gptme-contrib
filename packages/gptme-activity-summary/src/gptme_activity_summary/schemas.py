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

# Linkify owner/repo#NNN references into GitHub URLs.
_GITHUB_REF_RE = re.compile(r"(?<![/\w])([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#(\d+)")


def _linkify(text: str) -> str:
    """Turn owner/repo#NNN references into markdown links."""
    return _GITHUB_REF_RE.sub(r"[\1#\2](https://github.com/\1/issues/\2)", text)


def _fmt_tokens(n: int) -> str:
    """Format a token count for human readability."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


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
class ModelUsage:
    """Per-model usage breakdown."""

    model: str
    sessions: int = 0
    tokens: int = 0
    cost: float = 0.0


@dataclass
class Interaction:
    """A human or social interaction."""

    type: str  # "github_review", "github_issue", "social_post", "conversation", "discord"
    person: str  # who was involved (e.g. "Erik", "greptile-bot", "@ErikBjare")
    summary: str  # what happened
    url: str = ""  # link to the interaction


@dataclass
class ExternalContribution:
    """A PR or contribution to a repo Bob doesn't own."""

    repo: str  # e.g. "gptme/gptme"
    title: str
    pr_number: int = 0
    status: str = ""  # "merged", "open", "closed"
    url: str = ""


@dataclass
class ExternalSignal:
    """An external event, news item, or ecosystem development."""

    source: str  # e.g. "RSS", "Hacker News", "Twitter", "journal"
    title: str
    relevance: str = ""  # why it matters to Bob's work
    url: str = ""


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
    # Per-model breakdown (model -> {sessions, tokens, cost})
    model_breakdown: list[ModelUsage] = field(default_factory=list)
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


def _format_model_table(metrics: "Metrics") -> list[str]:
    """Format per-model usage as a markdown table. Returns lines or empty list."""
    if not metrics.model_breakdown:
        return []

    lines = [
        "## Model Usage",
        "",
        "| Model | Sessions | Tokens | Cost |",
        "|-------|----------|--------|------|",
    ]
    for m in metrics.model_breakdown:
        cost_str = f"${m.cost:.2f}" if m.cost > 0 else "-"
        tokens_str = _fmt_tokens(m.tokens) if m.tokens > 0 else "-"
        lines.append(f"| {m.model} | {m.sessions} | {tokens_str} | {cost_str} |")

    # Total row
    total_sessions = sum(m.sessions for m in metrics.model_breakdown)
    total_tokens = sum(m.tokens for m in metrics.model_breakdown)
    total_cost = sum(m.cost for m in metrics.model_breakdown)
    cost_str = f"${total_cost:.2f}" if total_cost > 0 else "-"
    tokens_str = _fmt_tokens(total_tokens) if total_tokens > 0 else "-"
    lines.append(f"| **Total** | **{total_sessions}** | **{tokens_str}** | **{cost_str}** |")
    lines.append("")
    return lines


def _format_session_metadata_line(metrics: "Metrics") -> str:
    """Compact one-line fallback when no per-model breakdown is available."""
    parts = []
    if metrics.model_breakdown:
        model_strs = [f"{m.model} ({m.sessions})" for m in metrics.model_breakdown]
        parts.append(f"**Models**: {', '.join(model_strs)}")
    if metrics.total_tokens:
        parts.append(f"**Tokens**: {_fmt_tokens(metrics.total_tokens)}")
    if metrics.total_cost > 0:
        parts.append(f"**Cost**: ${metrics.total_cost:.2f}")
    return " | ".join(parts) if parts else ""


def _render_interactions(interactions: list["Interaction"]) -> list[str]:
    """Render interactions section."""
    if not interactions:
        return []
    lines = ["## Interactions", ""]
    for i in interactions:
        label = i.type.replace("_", " ").title()
        entry = f"- **{label}** ({i.person}): {i.summary}"
        if i.url:
            entry += f" ([link]({i.url}))"
        lines.append(entry)
    lines.append("")
    return lines


def _render_external_contributions(contribs: list["ExternalContribution"]) -> list[str]:
    """Render external contributions section."""
    if not contribs:
        return []
    lines = ["## External Contributions", ""]
    for c in contribs:
        status = f" ({c.status})" if c.status else ""
        ref = f"{c.repo}#{c.pr_number}" if c.pr_number else c.repo
        entry = f"- **{ref}**{status}: {c.title}"
        if c.url:
            entry += f" ([link]({c.url}))"
        lines.append(entry)
    lines.append("")
    return lines


def _render_external_signals(signals: list["ExternalSignal"]) -> list[str]:
    """Render external signals / situational awareness section."""
    if not signals:
        return []
    lines = ["## External Signals", ""]
    for s in signals:
        entry = f"- **{s.source}**: {s.title}"
        if s.relevance:
            entry += f" — *{s.relevance}*"
        if s.url:
            entry += f" ([link]({s.url}))"
        lines.append(entry)
    lines.append("")
    return lines


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
    interactions: list[Interaction] = field(default_factory=list)
    external_signals: list[ExternalSignal] = field(default_factory=list)
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

        # Model table or fallback line
        model_table = _format_model_table(self.metrics)
        if model_table:
            lines.extend(model_table)
        else:
            meta = _format_session_metadata_line(self.metrics)
            if meta:
                lines.extend([meta, ""])

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

        lines.extend(_render_interactions(self.interactions))
        lines.extend(_render_external_signals(self.external_signals))

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
    progress_on_tasks: dict[str, str] = field(default_factory=dict)
    blockers_summary: str = ""
    metrics: Metrics = field(default_factory=Metrics)
    narrative: str = ""
    trends: list[str] = field(default_factory=list)
    interactions: list[Interaction] = field(default_factory=list)
    external_contributions: list[ExternalContribution] = field(default_factory=list)
    external_signals: list[ExternalSignal] = field(default_factory=list)
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

        model_table = _format_model_table(self.metrics)
        if model_table:
            lines.extend(model_table)
        else:
            meta = _format_session_metadata_line(self.metrics)
            if meta:
                lines.extend([meta, ""])

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

        lines.extend(_render_interactions(self.interactions))
        lines.extend(_render_external_contributions(self.external_contributions))
        lines.extend(_render_external_signals(self.external_signals))

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
    accomplishments: list[str] = field(default_factory=list)
    goals_progress: dict[str, str] = field(default_factory=dict)
    key_learnings: list[str] = field(default_factory=list)
    strategic_decisions: list[Decision] = field(default_factory=list)
    direction_for_next_month: list[str] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    month_narrative: str = ""
    highlights: list[str] = field(default_factory=list)
    external_contributions: list[ExternalContribution] = field(default_factory=list)
    external_signals: list[ExternalSignal] = field(default_factory=list)
    generated_at: Optional[datetime] = None

    def to_markdown(self) -> str:
        """Render as human-readable markdown."""
        lines = [
            f"# Monthly Summary: {self.month}",
            "",
        ]

        metrics_line = _format_metrics_line(self.metrics)
        if metrics_line:
            lines.extend([metrics_line, ""])

        model_table = _format_model_table(self.metrics)
        if model_table:
            lines.extend(model_table)
        else:
            meta = _format_session_metadata_line(self.metrics)
            if meta:
                lines.extend([meta, ""])

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

        lines.extend(_render_external_contributions(self.external_contributions))
        lines.extend(_render_external_signals(self.external_signals))

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
