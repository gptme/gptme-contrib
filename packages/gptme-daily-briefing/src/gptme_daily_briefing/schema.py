"""Bundle schema — the durable JSON contract between collectors and renderers.

The bundle is written by a `collect-daily-briefing` step and read by
both email and voice renderers, so neither re-queries upstream data.

Agent-specific extensions (e.g. Bob's bandit tops, KPI snapshots) live under
the open-shape `analytics` and `workstream` namespaces — those values are
typed `dict[str, Any]` so each agent can attach its own structures without
changing this schema.
"""

from __future__ import annotations

from typing import Any, TypedDict


class WaitingTask(TypedDict):
    """A task in `state: waiting` with its `waiting_for` blocker."""

    task: str
    waiting_for: str


class Bullets(TypedDict, total=False):
    """The `bullets` namespace — short lists for the morning summary."""

    blockers: list[str]
    active_tasks: list[str]
    waiting_tasks: list[WaitingTask]
    recent_highlights: list[str]


class SessionStats(TypedDict, total=False):
    """Session count + per-category breakdown over a recent window."""

    count: int
    categories: dict[str, int]
    error: str


class Analytics(TypedDict, total=False):
    """Analytics snapshot: sessions, agent-specific bandits/KPI."""

    sessions_24h: SessionStats
    bandits: dict[str, Any]
    kpi: dict[str, Any]


class OpenPR(TypedDict):
    """A single open PR by the agent."""

    repo: str
    number: int
    title: str
    draft: bool
    url: str


class Workstream(TypedDict, total=False):
    """In-flight work: open PRs, optional rich review-guide payload."""

    open_prs: list[OpenPR]
    open_prs_guide: dict[str, Any] | None


class BriefingBundle(TypedDict, total=False):
    """Top-level bundle — the durable artifact at `state/daily-briefing/<date>.json`."""

    generated_at: str
    date: str
    bullets: Bullets
    analytics: Analytics
    workstream: Workstream
