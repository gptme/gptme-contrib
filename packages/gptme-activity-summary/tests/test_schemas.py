"""Tests for schemas and generator modules."""

import json
from datetime import date


from gptme_activity_summary.schemas import (
    BlockerStatus,
    Blocker,
    Decision,
    DailySummary,
    ExternalContribution,
    ExternalSignal,
    Interaction,
    Metrics,
    ModelUsage,
    MonthlySummary,
    WeeklySummary,
    _fmt_tokens,
    _linkify,
)


# ---------------------------------------------------------------------------
# _linkify
# ---------------------------------------------------------------------------


def test_linkify_basic():
    text = "see gptme/gptme#123 for details"
    result = _linkify(text)
    assert "[gptme/gptme#123](https://github.com/gptme/gptme/issues/123)" in result


def test_linkify_multiple_refs():
    text = "gptme/gptme#1 and ActivityWatch/activitywatch#99"
    result = _linkify(text)
    assert "[gptme/gptme#1]" in result
    assert "[ActivityWatch/activitywatch#99]" in result


def test_linkify_no_ref():
    text = "no references here"
    assert _linkify(text) == text


def test_linkify_negative_lookbehind_url():
    # Ref inside a URL path should not be re-linkified
    text = "https://github.com/gptme/gptme/issues/123"
    result = _linkify(text)
    # The regex negative lookbehind (?<![/\w]) prevents matching inside a URL
    # The slash before 'gptme' blocks the match, so the URL stays intact
    assert "https://github.com/gptme/gptme/issues/123" in result


def test_linkify_already_linked():
    # Already a markdown link — the inner text still matches but that's fine
    text = "[gptme/gptme#1](https://github.com/gptme/gptme/issues/1)"
    result = _linkify(text)
    # Should not double-wrap; the text still contains the original URL
    assert "https://github.com/gptme/gptme/issues/1" in result


def test_linkify_underscore_in_name():
    text = "some_org/some_repo#42 is the issue"
    result = _linkify(text)
    assert "[some_org/some_repo#42](https://github.com/some_org/some_repo/issues/42)" in result


# ---------------------------------------------------------------------------
# _fmt_tokens
# ---------------------------------------------------------------------------


def test_fmt_tokens_small():
    assert _fmt_tokens(0) == "0"
    assert _fmt_tokens(999) == "999"


def test_fmt_tokens_thousands():
    assert _fmt_tokens(1_000) == "1.0K"
    assert _fmt_tokens(1_500) == "1.5K"
    assert _fmt_tokens(999_999) == "1000.0K"


def test_fmt_tokens_millions():
    assert _fmt_tokens(1_000_000) == "1.0M"
    assert _fmt_tokens(2_500_000) == "2.5M"


# ---------------------------------------------------------------------------
# Metrics + ModelUsage
# ---------------------------------------------------------------------------


def test_metrics_defaults():
    m = Metrics()
    assert m.sessions == 0
    assert m.commits == 0
    assert m.model_breakdown == []
    assert m.total_cost == 0.0


def test_model_usage_fields():
    mu = ModelUsage(model="claude-sonnet-4", sessions=3, tokens=5000, cost=0.05)
    assert mu.model == "claude-sonnet-4"
    assert mu.sessions == 3
    assert mu.tokens == 5000
    assert mu.cost == 0.05


# ---------------------------------------------------------------------------
# DailySummary.to_markdown
# ---------------------------------------------------------------------------


def test_daily_summary_minimal():
    ds = DailySummary(date=date(2026, 7, 1))
    md = ds.to_markdown()
    assert "# Daily Summary: 2026-07-01" in md


def test_daily_summary_accomplishments():
    ds = DailySummary(
        date=date(2026, 7, 1),
        accomplishments=["Shipped windowed open", "Fixed scroll anchor"],
    )
    md = ds.to_markdown()
    assert "## Key Accomplishments" in md
    assert "- Shipped windowed open" in md
    assert "- Fixed scroll anchor" in md


def test_daily_summary_decisions():
    ds = DailySummary(
        date=date(2026, 7, 1),
        decisions=[Decision(topic="Auth", decision="Use OAuth", rationale="Standard")],
    )
    md = ds.to_markdown()
    assert "## Decisions Made" in md
    assert "**Auth**: Use OAuth" in md


def test_daily_summary_active_blockers():
    ds = DailySummary(
        date=date(2026, 7, 1),
        blockers=[
            Blocker(issue="CI broken", status=BlockerStatus.ACTIVE),
            Blocker(issue="PR merged", status=BlockerStatus.RESOLVED, resolution="Fixed"),
        ],
    )
    md = ds.to_markdown()
    assert "## Active Blockers" in md
    assert "CI broken" in md
    assert "## Resolved" in md
    assert "PR merged" in md


def test_daily_summary_metrics_line():
    ds = DailySummary(
        date=date(2026, 7, 1),
        session_count=5,
        metrics=Metrics(commits=12, prs_merged=2),
    )
    md = ds.to_markdown()
    assert "**Sessions**: 5" in md
    assert "**Commits**: 12" in md
    assert "**PRs merged**: 2" in md


def test_daily_summary_model_table():
    ds = DailySummary(
        date=date(2026, 7, 1),
        metrics=Metrics(
            model_breakdown=[
                ModelUsage(
                    model="claude-sonnet-4",
                    harness="claude-code",
                    sessions=3,
                    tokens=10000,
                    cost=0.10,
                ),
                ModelUsage(
                    model="claude-opus-4", harness="gptme", sessions=1, tokens=5000, cost=0.20
                ),
            ]
        ),
    )
    md = ds.to_markdown()
    assert "## Model Usage" in md
    assert "claude-sonnet-4" in md
    assert "claude-opus-4" in md
    assert "**Total**" in md
    # Multi-harness — harness column should appear
    assert "Harness" in md


def test_daily_summary_single_harness_model_table():
    """Single harness: harness column should be omitted."""
    ds = DailySummary(
        date=date(2026, 7, 1),
        metrics=Metrics(
            model_breakdown=[
                ModelUsage(
                    model="claude-sonnet-4",
                    harness="claude-code",
                    sessions=2,
                    tokens=3000,
                    cost=0.03,
                ),
            ]
        ),
    )
    md = ds.to_markdown()
    assert "## Model Usage" in md
    assert "Harness" not in md


def test_daily_summary_interactions():
    ds = DailySummary(
        date=date(2026, 7, 1),
        interactions=[
            Interaction(
                type="github_review",
                person="Erik",
                summary="Reviewed PR #123",
                url="https://github.com/x/y/pull/1",
            ),
        ],
    )
    md = ds.to_markdown()
    assert "## Interactions" in md
    assert "Github Review" in md
    assert "Erik" in md
    assert "https://github.com/x/y/pull/1" in md


def test_daily_summary_external_signals():
    ds = DailySummary(
        date=date(2026, 7, 1),
        external_signals=[
            ExternalSignal(
                source="HN",
                title="Interesting post",
                relevance="agent-related",
                url="https://hn.com/1",
            ),
        ],
    )
    md = ds.to_markdown()
    assert "## External Signals" in md
    assert "HN" in md
    assert "agent-related" in md


def test_daily_summary_key_insight():
    ds = DailySummary(date=date(2026, 7, 1), key_insight="Triage volume is the root cause")
    md = ds.to_markdown()
    assert "## Key Insight" in md
    assert "Triage volume is the root cause" in md


def test_daily_summary_linkifies_github_refs():
    ds = DailySummary(
        date=date(2026, 7, 1),
        accomplishments=["fixed gptme/gptme#3034"],
    )
    md = ds.to_markdown()
    assert "[gptme/gptme#3034](https://github.com/gptme/gptme/issues/3034)" in md


# ---------------------------------------------------------------------------
# WeeklySummary.to_markdown
# ---------------------------------------------------------------------------


def test_weekly_summary_minimal():
    ws = WeeklySummary(
        week="2026-W27",
        start_date=date(2026, 6, 29),
        end_date=date(2026, 7, 5),
    )
    md = ws.to_markdown()
    assert "# Weekly Summary: 2026-W27" in md
    assert "2026-06-29 to 2026-07-05" in md


def test_weekly_summary_milestones():
    ws = WeeklySummary(
        week="2026-W27",
        start_date=date(2026, 6, 29),
        end_date=date(2026, 7, 5),
        milestones=["Shipped pagination", "Merged 5 PRs"],
    )
    md = ws.to_markdown()
    assert "## Major Milestones" in md
    assert "- Shipped pagination" in md


def test_weekly_summary_external_contributions():
    ws = WeeklySummary(
        week="2026-W27",
        start_date=date(2026, 6, 29),
        end_date=date(2026, 7, 5),
        external_contributions=[
            ExternalContribution(
                repo="gptme/gptme",
                title="Windowed open",
                pr_number=3034,
                status="open",
                url="https://github.com/gptme/gptme/pull/3034",
            ),
        ],
    )
    md = ws.to_markdown()
    assert "## External Contributions" in md
    assert "gptme/gptme#3034" in md
    assert "(open)" in md


def test_weekly_summary_task_progress():
    ws = WeeklySummary(
        week="2026-W27",
        start_date=date(2026, 6, 29),
        end_date=date(2026, 7, 5),
        progress_on_tasks={"pagination": "merged", "auth": "in progress"},
    )
    md = ws.to_markdown()
    assert "## Task Progress" in md
    assert "**pagination**: merged" in md
    assert "**auth**: in progress" in md


# ---------------------------------------------------------------------------
# MonthlySummary.to_markdown
# ---------------------------------------------------------------------------


def test_monthly_summary_minimal():
    ms = MonthlySummary(month="2026-07")
    md = ms.to_markdown()
    assert "# Monthly Summary: 2026-07" in md


def test_monthly_summary_accomplishments():
    ms = MonthlySummary(
        month="2026-07",
        accomplishments=["Shipped gptme-cloud staging"],
        key_learnings=["Fanout load gate prevents OOM"],
    )
    md = ms.to_markdown()
    assert "## Major Accomplishments" in md
    assert "Shipped gptme-cloud staging" in md
    assert "## Key Learnings" in md
    assert "Fanout load gate prevents OOM" in md


def test_monthly_summary_goals_progress():
    ms = MonthlySummary(
        month="2026-07",
        goals_progress={"reduce PR queue": "done", "ship cloud": "partial"},
    )
    md = ms.to_markdown()
    assert "## Goals Progress" in md
    assert "**reduce PR queue**: done" in md


def test_monthly_summary_direction():
    ms = MonthlySummary(
        month="2026-07",
        direction_for_next_month=["Focus on user testing", "Improve quality KPI"],
    )
    md = ms.to_markdown()
    assert "## Direction for Next Month" in md
    assert "Focus on user testing" in md


# ---------------------------------------------------------------------------
# BlockerStatus enum
# ---------------------------------------------------------------------------


def test_blocker_status_values():
    assert BlockerStatus.ACTIVE.value == "active"
    assert BlockerStatus.RESOLVED.value == "resolved"
    assert BlockerStatus.ESCALATED.value == "escalated"
    assert BlockerStatus.DEFERRED.value == "deferred"


# ---------------------------------------------------------------------------
# generator: get_journal_entries_for_date + save_summary
# ---------------------------------------------------------------------------


def test_get_journal_entries_for_date_empty(tmp_path, monkeypatch):
    """No entries for a date returns empty list."""
    monkeypatch.setattr("gptme_activity_summary.generator.JOURNAL_DIR", tmp_path)
    from gptme_activity_summary.generator import get_journal_entries_for_date

    result = get_journal_entries_for_date(date(2026, 7, 1))
    assert result == []


def test_get_journal_entries_for_date_new_format(tmp_path, monkeypatch):
    """New journal format: journal/YYYY-MM-DD/*.md."""
    monkeypatch.setattr("gptme_activity_summary.generator.JOURNAL_DIR", tmp_path)
    from gptme_activity_summary.generator import get_journal_entries_for_date

    date_dir = tmp_path / "2026-07-01"
    date_dir.mkdir()
    (date_dir / "autonomous-session-a1b2.md").write_text("# Session a1b2")
    (date_dir / "project-monitoring.md").write_text("# PM")

    result = get_journal_entries_for_date(date(2026, 7, 1))
    assert len(result) == 2
    names = [p.name for p in result]
    assert "autonomous-session-a1b2.md" in names
    assert "project-monitoring.md" in names


def test_get_journal_entries_for_date_old_format(tmp_path, monkeypatch):
    """Old journal format: journal/YYYY-MM-DD-topic.md."""
    monkeypatch.setattr("gptme_activity_summary.generator.JOURNAL_DIR", tmp_path)
    from gptme_activity_summary.generator import get_journal_entries_for_date

    (tmp_path / "2026-07-01-session.md").write_text("# old format")
    (tmp_path / "2026-07-02-other.md").write_text("# different date")

    result = get_journal_entries_for_date(date(2026, 7, 1))
    assert len(result) == 1
    assert result[0].name == "2026-07-01-session.md"


def test_get_journal_entries_sorted(tmp_path, monkeypatch):
    """Entries are returned sorted by path."""
    monkeypatch.setattr("gptme_activity_summary.generator.JOURNAL_DIR", tmp_path)
    from gptme_activity_summary.generator import get_journal_entries_for_date

    date_dir = tmp_path / "2026-07-01"
    date_dir.mkdir()
    (date_dir / "z-session.md").write_text("z")
    (date_dir / "a-session.md").write_text("a")

    result = get_journal_entries_for_date(date(2026, 7, 1))
    assert result[0].name == "a-session.md"
    assert result[1].name == "z-session.md"


def test_save_daily_summary(tmp_path, monkeypatch):
    """save_summary writes both .md and .json for a DailySummary."""
    monkeypatch.setattr("gptme_activity_summary.generator.SUMMARIES_DIR", tmp_path)
    from gptme_activity_summary.generator import save_summary

    ds = DailySummary(date=date(2026, 7, 1), accomplishments=["shipped X"])
    path = save_summary(ds)

    assert path.suffix == ".md"
    assert path.exists()
    content = path.read_text()
    assert "# Daily Summary: 2026-07-01" in content

    json_path = path.with_suffix(".json")
    assert json_path.exists()
    data = json.loads(json_path.read_text())
    assert data["date"] == "2026-07-01"
    assert "shipped X" in data["accomplishments"]


def test_save_weekly_summary(tmp_path, monkeypatch):
    """save_summary writes both .md and .json for a WeeklySummary."""
    monkeypatch.setattr("gptme_activity_summary.generator.SUMMARIES_DIR", tmp_path)
    from gptme_activity_summary.generator import save_summary

    ws = WeeklySummary(week="2026-W27", start_date=date(2026, 6, 29), end_date=date(2026, 7, 5))
    path = save_summary(ws)

    assert path.name == "2026-W27.md"
    assert "weekly" in str(path)
    json_path = path.with_suffix(".json")
    data = json.loads(json_path.read_text())
    assert data["week"] == "2026-W27"


def test_save_monthly_summary(tmp_path, monkeypatch):
    """save_summary writes both .md and .json for a MonthlySummary."""
    monkeypatch.setattr("gptme_activity_summary.generator.SUMMARIES_DIR", tmp_path)
    from gptme_activity_summary.generator import save_summary

    ms = MonthlySummary(month="2026-07")
    path = save_summary(ms)

    assert path.name == "2026-07.md"
    assert "monthly" in str(path)


def test_save_summary_creates_output_dir(tmp_path, monkeypatch):
    """save_summary creates output dirs if they don't exist."""
    summaries_dir = tmp_path / "nonexistent" / "summaries"
    monkeypatch.setattr("gptme_activity_summary.generator.SUMMARIES_DIR", summaries_dir)
    from gptme_activity_summary.generator import save_summary

    ds = DailySummary(date=date(2026, 7, 1))
    path = save_summary(ds)
    assert path.exists()
