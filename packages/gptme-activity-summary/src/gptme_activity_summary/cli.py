"""
CLI for activity summarization — journals, GitHub, sessions, tweets, email.

Supports two modes:

Agent mode (journal-based, default):
    summarize daily [--date DATE]
    summarize weekly [--week WEEK]
    summarize monthly [--month MONTH]
    summarize smart [--date DATE]  # Daily job that auto-runs weekly/monthly when due
    summarize backfill [--from DATE] [--to DATE]
    summarize stats

Human mode (AW time tracking + optional GitHub):
    summarize daily --mode human [--date DATE] [--github-user USER] [--raw]
    summarize weekly --mode human [--week WEEK] [--github-user USER] [--raw]
    summarize monthly --mode human [--month MONTH] [--github-user USER] [--raw]

All summarization uses Claude Code backend for high-quality results.
"""

import json
from datetime import date, datetime, timedelta

import click

from .cc_session_data import fetch_cc_session_stats_range
from .generator import (
    JOURNAL_DIR,
    SUMMARIES_DIR,
    WORKSPACE,
    get_journal_entries_for_date,
    save_summary,
)
from .github_data import (
    GitHubActivity,
    fetch_activity,
    fetch_user_activity,
    format_activity_for_prompt,
)
from .schemas import (
    Blocker,
    BlockerStatus,
    DailySummary,
    Decision,
    ExternalContribution,
    ExternalSignal,
    Interaction,
    Metrics,
    ModelUsage,
    MonthlySummary,
)
from .session_data import (
    SessionStats,
    fetch_session_stats,
    fetch_session_stats_range,
    format_sessions_for_prompt,
    merge_session_stats,
)
from .aw_data import (
    fetch_aw_activity,
    format_aw_activity_for_prompt,
)
from .workspace_data import (
    fetch_workspace_activity,
    format_workspace_activity_for_prompt,
)


def _parse_blocker_status(status: str) -> BlockerStatus:
    """Parse blocker status string, defaulting to ACTIVE for unknown values."""
    try:
        return BlockerStatus(status)
    except ValueError:
        return BlockerStatus.ACTIVE


def _build_model_breakdown(session_stats):  # type: ignore[no-untyped-def]
    """Build ModelUsage list from SessionStats."""
    return [
        ModelUsage(
            model=mb.model,
            harness=mb.harness,
            sessions=mb.sessions,
            tokens=mb.total_tokens,
            cost=mb.cost,
        )
        for mb in session_stats.model_breakdown
    ]


def _build_interactions_from_result(result: dict) -> list[Interaction]:
    """Build Interaction list from LLM result dict."""
    interactions = []
    for i in result.get("interactions", []):
        if isinstance(i, dict):
            interactions.append(
                Interaction(
                    type=i.get("type", "conversation"),
                    person=i.get("person", ""),
                    summary=i.get("summary", ""),
                    url=i.get("url", ""),
                )
            )
    return interactions


def _build_interactions_from_github(activity) -> list[Interaction]:  # type: ignore[no-untyped-def]
    """Build Interaction list from GitHub reviews received."""
    return [
        Interaction(
            type="github_review",
            person=r.reviewer,
            summary=f"Reviewed {r.repo}#{r.pr_number}: {r.pr_title}",
            url=r.url,
        )
        for r in activity.reviews_received
    ]


def _build_external_contributions(activity) -> list[ExternalContribution]:  # type: ignore[no-untyped-def]
    """Build ExternalContribution list from cross-repo PRs."""
    return [
        ExternalContribution(
            repo=pr.repo,
            title=pr.title,
            pr_number=pr.number,
            status=pr.state,
            url=pr.url,
        )
        for pr in activity.cross_repo_prs
    ]


def _build_external_signals(result: dict) -> list[ExternalSignal]:
    """Build ExternalSignal list from LLM result dict."""
    signals = []
    for s in result.get("external_signals", []):
        if isinstance(s, dict):
            signals.append(
                ExternalSignal(
                    source=s.get("source", "journal"),
                    title=s.get("title", ""),
                    relevance=s.get("relevance", ""),
                    url=s.get("url", ""),
                )
            )
    return signals


def _fetch_data(
    start: date,
    end: date,
) -> tuple[GitHubActivity, SessionStats]:
    """Fetch GitHub activity and session stats for a date range.

    Merges gptme session stats with Claude Code session stats.
    """
    activity = fetch_activity(start, end, workspace=str(WORKSPACE))
    if start == end:
        gptme_stats = fetch_session_stats(start)
    else:
        gptme_stats = fetch_session_stats_range(start, end)

    # Merge in Claude Code sessions
    cc_stats = fetch_cc_session_stats_range(start, end)
    session_stats = merge_session_stats(gptme_stats, cc_stats)

    return activity, session_stats


def _build_extra_context(
    start: date,
    end: date,
    activity: GitHubActivity,
    session_stats: SessionStats,
    verbose: bool = False,
) -> str:
    """Build extra context string from all available data sources.

    Sources:
    - GitHub activity (commits, PRs, issues via gh CLI)
    - gptme session stats (models, tokens, cost from log files)
    - Workspace activity (posted tweets, sent emails from workspace dirs)
    - ActivityWatch time tracking (if AW server is running)
    """
    parts: list[str] = []

    activity_text = format_activity_for_prompt(activity)
    if activity_text:
        parts.append(activity_text)
        if verbose:
            click.echo(
                f"  GitHub: {activity.total_commits} commits, {activity.total_prs_merged} PRs, {activity.total_issues_closed} issues"
            )

    session_text = format_sessions_for_prompt(session_stats)
    if session_text:
        parts.append(session_text)
        if verbose:
            click.echo(
                f"  Sessions: {session_stats.session_count}, tokens: {session_stats.total_tokens:,}"
            )

    # Fetch workspace activity (tweets, emails)
    ws_activity = fetch_workspace_activity(start, end, WORKSPACE)
    ws_text = format_workspace_activity_for_prompt(ws_activity)
    if ws_text:
        parts.append(ws_text)
        if verbose:
            click.echo(
                f"  Workspace: {len(ws_activity.tweets)} tweets, {len(ws_activity.emails)} emails"
            )

    # Fetch ActivityWatch time tracking data (optional, graceful fallback)
    aw_activity = fetch_aw_activity(start, end)
    aw_text = format_aw_activity_for_prompt(aw_activity)
    if aw_text:
        parts.append(aw_text)
        if verbose:
            domain_info = (
                f", {len(aw_activity.top_domains)} domains" if aw_activity.top_domains else ""
            )
            click.echo(
                f"  ActivityWatch: {aw_activity.total_active_hours:.1f}h active"
                f", {len(aw_activity.top_apps)} apps{domain_info}"
            )

    return "\n".join(parts)


def _load_daily_summary_dict(daily_path_base: str) -> dict[str, object] | None:
    """Try to load a daily summary as a dict, preferring JSON over markdown parsing."""
    from pathlib import Path

    # Prefer JSON file
    json_path = Path(daily_path_base).with_suffix(".json")
    if json_path.exists():
        try:
            with open(json_path) as f:
                data: dict[str, object] = json.load(f)
                return data
        except (json.JSONDecodeError, OSError):
            pass

    # Fall back to markdown parsing
    md_path = Path(daily_path_base).with_suffix(".md")
    if md_path.exists():
        content = md_path.read_text()
        return {
            "date": Path(daily_path_base).stem,
            "accomplishments": extract_list_from_md(content, "Accomplishments"),
            "themes": extract_list_from_md(content, "Themes"),
            "key_insight": extract_single_from_md(content, "Key Insight"),
            "decisions": [],
            "blockers": [],
            "work_in_progress": extract_list_from_md(content, "Work in Progress"),
        }
    return None


def _load_weekly_summary_dict(weekly_path_base: str) -> dict[str, object] | None:
    """Try to load a weekly summary as a dict, preferring JSON over markdown parsing."""
    from pathlib import Path

    # Prefer JSON file
    json_path = Path(weekly_path_base).with_suffix(".json")
    if json_path.exists():
        try:
            with open(json_path) as f:
                data: dict[str, object] = json.load(f)
                return data
        except (json.JSONDecodeError, OSError):
            pass

    # Fall back to markdown parsing
    md_path = Path(weekly_path_base).with_suffix(".md")
    if md_path.exists():
        content = md_path.read_text()
        milestones = extract_list_from_md(content, "Major Milestones")
        if not milestones:
            milestones = extract_list_from_md(content, "Milestones")
        return {
            "week": Path(weekly_path_base).stem,
            "milestones": milestones,
            "top_accomplishments": milestones,
            "recurring_themes": extract_list_from_md(content, "Recurring Themes"),
            "themes": extract_list_from_md(content, "Recurring Themes"),
            "trends": extract_list_from_md(content, "Observed Trends"),
            "patterns": extract_list_from_md(content, "Observed Trends"),
            "key_decisions": [],
            "weekly_insight": "",
        }
    return None


def generate_daily_with_cc(target_date: date, verbose: bool = False) -> DailySummary:
    """Generate daily summary using Claude Code backend."""
    from .cc_backend import summarize_daily_with_cc

    entries = get_journal_entries_for_date(target_date)
    if not entries:
        raise ValueError(f"No entries for {target_date}")

    # Load content for each entry
    entry_contents = [(p, p.read_text()) for p in entries]

    # Fetch data once, use for both prompt context and metrics
    if verbose:
        click.echo("Fetching real data sources...")
    activity, session_stats = _fetch_data(target_date, target_date)
    extra_context = _build_extra_context(
        target_date, target_date, activity, session_stats, verbose=verbose
    )

    # Get summary from Claude Code
    result = summarize_daily_with_cc(entry_contents, str(target_date), extra_context=extra_context)

    # Build interactions from LLM + GitHub reviews
    interactions = _build_interactions_from_result(result)
    interactions.extend(_build_interactions_from_github(activity))

    # Convert to DailySummary schema with real metrics
    return DailySummary(
        date=target_date,
        session_count=session_stats.session_count
        if session_stats.session_count > 0
        else len(entries),
        accomplishments=result.get("accomplishments", [])[:10],
        decisions=[
            Decision(
                topic=d.get("topic", ""),
                decision=d.get("decision", ""),
                rationale=d.get("rationale", ""),
            )
            for d in result.get("decisions", [])[:5]
        ],
        blockers=[
            Blocker(
                issue=b.get("issue", ""),
                status=_parse_blocker_status(b.get("status", "active")),
            )
            for b in result.get("blockers", [])
        ],
        themes=result.get("themes", []),
        work_in_progress=result.get("work_in_progress", [])[:5],
        narrative=result.get("narrative", ""),
        key_insight=result.get("key_insight", ""),
        interactions=interactions,
        external_signals=_build_external_signals(result),
        metrics=Metrics(
            sessions=session_stats.session_count,
            commits=activity.total_commits,
            prs_merged=activity.total_prs_merged,
            issues_closed=activity.total_issues_closed,
            model_breakdown=_build_model_breakdown(session_stats),
            total_tokens=session_stats.total_tokens,
            total_cost=session_stats.total_cost,
        ),
        generated_at=datetime.utcnow(),
    )


def generate_weekly_summary_cc(week: str, verbose: bool = False):
    """Generate weekly summary using Claude Code backend."""
    from .cc_backend import summarize_weekly_with_cc
    from .schemas import Decision, Metrics, WeeklySummary

    # Parse week string
    year, week_num = int(week[:4]), int(week[6:])

    # Calculate start and end dates for this week
    jan1 = date(year, 1, 1)
    first_monday = jan1 + timedelta(days=(7 - jan1.weekday()) % 7)
    if jan1.weekday() <= 3:
        first_monday = jan1 - timedelta(days=jan1.weekday())

    start_date = first_monday + timedelta(weeks=week_num - 1)
    end_date = start_date + timedelta(days=6)

    # Load daily summaries — prefer JSON, fall back to markdown
    daily_summaries = []
    current_date = start_date
    while current_date <= end_date:
        daily_path_base = str(SUMMARIES_DIR / "daily" / current_date.isoformat())
        summary_dict = _load_daily_summary_dict(daily_path_base)
        if summary_dict:
            # Ensure date is set
            if "date" not in summary_dict:
                summary_dict["date"] = current_date.isoformat()
            daily_summaries.append(summary_dict)
        current_date += timedelta(days=1)

    if not daily_summaries:
        raise ValueError(f"No daily summaries found for {week}")

    # Fetch data once, use for both prompt context and metrics
    if verbose:
        click.echo("Fetching real data sources...")
    activity, session_stats = _fetch_data(start_date, end_date)
    extra_context = _build_extra_context(
        start_date, end_date, activity, session_stats, verbose=verbose
    )

    # Call Claude Code backend with full daily context
    cc_result = summarize_weekly_with_cc(daily_summaries, week, extra_context=extra_context)

    # Convert to WeeklySummary schema
    decisions = []
    for d in cc_result.get("key_decisions", []):
        if isinstance(d, dict):
            decisions.append(
                Decision(
                    topic=d.get("topic", ""),
                    decision=d.get("decision", ""),
                    rationale=d.get("impact", "") or d.get("rationale", "") or "",
                )
            )

    return WeeklySummary(
        week=week,
        start_date=start_date,
        end_date=end_date,
        milestones=cc_result.get("top_accomplishments", []),
        recurring_themes=cc_result.get("themes", []),
        key_decisions=decisions,
        trends=cc_result.get("patterns", []),
        narrative=cc_result.get("narrative", ""),
        interactions=_build_interactions_from_github(activity),
        external_contributions=_build_external_contributions(activity),
        metrics=Metrics(
            sessions=session_stats.session_count,
            commits=activity.total_commits,
            prs_merged=activity.total_prs_merged,
            issues_closed=activity.total_issues_closed,
            model_breakdown=_build_model_breakdown(session_stats),
            total_tokens=session_stats.total_tokens,
            total_cost=session_stats.total_cost,
        ),
        generated_at=datetime.utcnow(),
    )


def extract_list_from_md(content: str, section: str) -> list[str]:
    """Extract a list from a markdown section."""
    import re

    # Try both formats: "## Section" and "**Section**"
    patterns = [
        rf"##\s*{section}\s*\n((?:- .+\n?)+)",  # ## Section format
        rf"##\s*Key\s*{section}\s*\n((?:- .+\n?)+)",  # ## Key Section format
        rf"\*\*{section}\*\*:?\s*\n((?:- .+\n?)+)",  # **Section** format
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return [
                line.strip("- ").strip()
                for line in match.group(1).strip().split("\n")
                if line.strip()
            ]
    return []


def extract_single_from_md(content: str, section: str) -> str:
    """Extract a single value from markdown."""
    import re

    patterns = [
        rf"##\s*{section}\s*\n(.+)",
        rf"\*\*{section}\*\*:?\s*(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def generate_monthly_summary_cc(month: str, verbose: bool = False):
    """Generate monthly summary using Claude Code backend."""
    from .cc_backend import summarize_monthly_with_cc
    from .schemas import Decision, Metrics

    # Parse month to get weeks
    year, month_num = int(month[:4]), int(month[5:])
    first_day = date(year, month_num, 1)
    if month_num == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month_num + 1, 1) - timedelta(days=1)

    # Load weekly summaries — prefer JSON, fall back to markdown
    weekly_summaries = []
    current = first_day
    weeks_processed: set[str] = set()
    while current <= last_day:
        week = current.strftime("%G-W%V")
        if week not in weeks_processed:
            weeks_processed.add(week)
            weekly_path_base = str(SUMMARIES_DIR / "weekly" / week)
            summary_dict = _load_weekly_summary_dict(weekly_path_base)
            if summary_dict:
                if "week" not in summary_dict:
                    summary_dict["week"] = week
                weekly_summaries.append(summary_dict)
        current += timedelta(days=1)

    if not weekly_summaries:
        raise ValueError(f"No weekly summaries found for {month}")

    # Fetch data once, use for both prompt context and metrics
    if verbose:
        click.echo("Fetching real data sources...")
    activity, session_stats = _fetch_data(first_day, last_day)
    extra_context = _build_extra_context(
        first_day, last_day, activity, session_stats, verbose=verbose
    )

    # Call Claude Code backend with full weekly context
    cc_result = summarize_monthly_with_cc(weekly_summaries, month, extra_context=extra_context)

    # Convert to MonthlySummary schema
    decisions = []
    for d in cc_result.get("strategic_decisions", []):
        if isinstance(d, dict):
            decisions.append(
                Decision(
                    topic=d.get("topic", ""),
                    decision=d.get("decision", ""),
                    rationale=d.get("strategic_impact", "") or d.get("rationale", "") or "",
                )
            )

    return MonthlySummary(
        month=month,
        accomplishments=cc_result.get("major_achievements", []),
        key_learnings=cc_result.get("key_learnings", []),
        strategic_decisions=decisions,
        month_narrative=cc_result.get("month_narrative", ""),
        external_contributions=_build_external_contributions(activity),
        metrics=Metrics(
            sessions=session_stats.session_count,
            commits=activity.total_commits,
            prs_merged=activity.total_prs_merged,
            issues_closed=activity.total_issues_closed,
            model_breakdown=_build_model_breakdown(session_stats),
            total_tokens=session_stats.total_tokens,
            total_cost=session_stats.total_cost,
        ),
        generated_at=datetime.utcnow(),
    )


def _parse_date_arg(date_str: str) -> date:
    """Parse a date argument that can be 'today', 'yesterday', or YYYY-MM-DD."""
    if date_str == "today":
        return date.today()
    elif date_str == "yesterday":
        return date.today() - timedelta(days=1)
    else:
        return date.fromisoformat(date_str)


def _run_human_summary(
    ctx: click.Context,
    start_date: date,
    end_date: date,
    period_str: str,
    period: str,
    github_user: str | None,
    raw: bool,
    verbose: bool,
) -> None:
    """Shared implementation for human mode across daily/weekly/monthly."""
    click.echo(f"Generating human activity summary for {period_str}...")

    parts: list[str] = []

    # ActivityWatch time tracking (primary source)
    aw_activity = fetch_aw_activity(start_date, end_date)
    aw_text = format_aw_activity_for_prompt(aw_activity)
    if aw_text:
        parts.append(aw_text)
        if verbose:
            domain_info = (
                f", {len(aw_activity.top_domains)} domains" if aw_activity.top_domains else ""
            )
            click.echo(
                f"  ActivityWatch: {aw_activity.total_active_hours:.1f}h active, "
                f"{len(aw_activity.top_apps)} apps{domain_info}"
            )
    elif not aw_activity.available:
        click.echo("  Note: ActivityWatch server not reachable — no time tracking data")

    # GitHub activity (optional)
    if github_user:
        gh_activity = fetch_user_activity(start_date, end_date, github_user)
        gh_text = format_activity_for_prompt(gh_activity)
        if gh_text:
            parts.append(gh_text)
            if verbose:
                click.echo(
                    f"  GitHub: {gh_activity.total_commits} commits, "
                    f"{gh_activity.total_prs_merged} PRs, "
                    f"{gh_activity.total_issues_closed} issues"
                )

    if not parts:
        click.echo("No activity data found for this period.")
        return

    combined_context = "\n".join(parts)

    if raw:
        click.echo(f"\n{combined_context}")
        return

    # Generate LLM summary
    if github_user and period != "daily":
        from .cc_backend import summarize_github_activity_with_cc

        click.echo("Generating summary with Claude Code...")
        result = summarize_github_activity_with_cc(combined_context, github_user, period_str)

        click.echo(f"\n## Activity Summary: @{github_user}")
        click.echo(f"**Period**: {period_str}\n")
    else:
        from .cc_backend import summarize_human_day_with_cc

        username = github_user or "user"
        click.echo("Generating summary with Claude Code...")
        result = summarize_human_day_with_cc(combined_context, username, period_str)

        click.echo(f"\n## Daily Summary: {period_str}")
        if github_user:
            click.echo(f"**GitHub**: @{github_user}\n")

    if result.get("narrative"):
        click.echo(result["narrative"])
        click.echo()

    if result.get("highlights"):
        click.echo("### Highlights")
        for h in result["highlights"]:
            click.echo(f"- {h}")
        click.echo()

    if result.get("time_breakdown"):
        click.echo("### Time Breakdown")
        for entry in result["time_breakdown"]:
            click.echo(f"- {entry}")
        click.echo()

    if result.get("projects_active"):
        click.echo("### Active Projects")
        for p in result["projects_active"]:
            desc = p.get("description", "")
            repo = p.get("repo", "")
            prs = p.get("prs", 0)
            issues = p.get("issues", 0)
            click.echo(f"- **{repo}**: {desc} ({prs} PRs, {issues} issues)")
        click.echo()

    if result.get("themes"):
        click.echo("### Themes")
        for t in result["themes"]:
            click.echo(f"- {t}")
        click.echo()

    stats_data = result.get("stats", {})
    if stats_data:
        click.echo("### Stats")
        click.echo(f"- Commits: {stats_data.get('total_commits', 0)}")
        click.echo(f"- PRs: {stats_data.get('total_prs', 0)}")
        click.echo(f"- Issues: {stats_data.get('total_issues', 0)}")
        click.echo(f"- Repos active: {stats_data.get('repos_active', 0)}")


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.option("--dry-run", is_flag=True, help="Print output without saving")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, dry_run: bool) -> None:
    """Activity summarization — journals, GitHub, sessions, tweets, email."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["dry_run"] = dry_run


@cli.command()
@click.option(
    "--date",
    "date_str",
    default="yesterday",
    help="Date to summarize (YYYY-MM-DD, 'today', or 'yesterday')",
)
@click.option(
    "--mode",
    type=click.Choice(["agent", "human"]),
    default="agent",
    help="Summary mode: agent (journal-based) or human (AW + GitHub)",
)
@click.option(
    "--github-user",
    default=None,
    help="GitHub username for human mode (optional)",
)
@click.option("--raw", is_flag=True, help="Print raw data without LLM summarization (human mode)")
@click.pass_context
def daily(ctx: click.Context, date_str: str, mode: str, github_user: str | None, raw: bool) -> None:
    """Generate daily summary."""
    verbose = ctx.obj["verbose"]
    dry_run = ctx.obj["dry_run"]
    target_date = _parse_date_arg(date_str)

    if mode == "human":
        _run_human_summary(
            ctx,
            start_date=target_date,
            end_date=target_date,
            period_str=target_date.isoformat(),
            period="daily",
            github_user=github_user,
            raw=raw,
            verbose=verbose,
        )
        return

    entries = get_journal_entries_for_date(target_date)
    if not entries:
        click.echo(f"No journal entries found for {target_date}")
        ctx.exit(1)
        return

    click.echo(f"Generating daily summary for {target_date} ({len(entries)} entries)...")

    summary = generate_daily_with_cc(target_date, verbose=verbose)

    if dry_run:
        click.echo("\n--- DRY RUN OUTPUT ---")
        click.echo(summary.to_markdown())
        return

    output_path = save_summary(summary)
    click.echo(f"Saved to {output_path}")

    if verbose:
        click.echo("\n--- Summary ---")
        click.echo(summary.to_markdown())


@cli.command()
@click.option(
    "--week",
    default="last",
    help="Week to summarize (YYYY-Www, 'current', or 'last')",
)
@click.option(
    "--mode",
    type=click.Choice(["agent", "human"]),
    default="agent",
    help="Summary mode: agent (journal-based) or human (AW + GitHub)",
)
@click.option(
    "--github-user",
    default=None,
    help="GitHub username for human mode (optional)",
)
@click.option("--raw", is_flag=True, help="Print raw data without LLM summarization (human mode)")
@click.pass_context
def weekly(ctx: click.Context, week: str, mode: str, github_user: str | None, raw: bool) -> None:
    """Generate weekly summary."""
    verbose = ctx.obj["verbose"]
    dry_run = ctx.obj["dry_run"]

    if week == "current":
        today = date.today()
        week = today.strftime("%G-W%V")
    elif week == "last":
        last_week = date.today() - timedelta(days=7)
        week = last_week.strftime("%G-W%V")

    if mode == "human":
        # Parse week string to date range
        year, week_num = int(week[:4]), int(week[6:])
        jan1 = date(year, 1, 1)
        first_monday = jan1 - timedelta(days=jan1.weekday())
        if jan1.weekday() > 3:
            first_monday += timedelta(weeks=1)
        start_date = first_monday + timedelta(weeks=week_num - 1)
        end_date = start_date + timedelta(days=6)
        _run_human_summary(
            ctx,
            start_date=start_date,
            end_date=end_date,
            period_str=f"{start_date.isoformat()} to {end_date.isoformat()}",
            period="weekly",
            github_user=github_user,
            raw=raw,
            verbose=verbose,
        )
        return

    click.echo(f"Generating weekly summary for {week}...")

    summary = generate_weekly_summary_cc(week, verbose=verbose)

    if dry_run:
        click.echo("\n--- DRY RUN OUTPUT ---")
        click.echo(summary.to_markdown())
        return

    output_path = save_summary(summary)
    click.echo(f"Saved to {output_path}")

    if verbose:
        click.echo("\n--- Summary ---")
        click.echo(summary.to_markdown())


@cli.command()
@click.option(
    "--month",
    default="last",
    help="Month to summarize (YYYY-MM, 'current', or 'last')",
)
@click.option(
    "--mode",
    type=click.Choice(["agent", "human"]),
    default="agent",
    help="Summary mode: agent (journal-based) or human (AW + GitHub)",
)
@click.option(
    "--github-user",
    default=None,
    help="GitHub username for human mode (optional)",
)
@click.option("--raw", is_flag=True, help="Print raw data without LLM summarization (human mode)")
@click.pass_context
def monthly(ctx: click.Context, month: str, mode: str, github_user: str | None, raw: bool) -> None:
    """Generate monthly summary."""
    verbose = ctx.obj["verbose"]
    dry_run = ctx.obj["dry_run"]

    if month == "current":
        month = date.today().strftime("%Y-%m")
    elif month == "last":
        first_of_month = date.today().replace(day=1)
        last_month = first_of_month - timedelta(days=1)
        month = last_month.strftime("%Y-%m")

    if mode == "human":
        year, month_num = int(month[:4]), int(month[5:])
        start_date = date(year, month_num, 1)
        if month_num == 12:
            end_date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(year, month_num + 1, 1) - timedelta(days=1)
        _run_human_summary(
            ctx,
            start_date=start_date,
            end_date=end_date,
            period_str=month,
            period="monthly",
            github_user=github_user,
            raw=raw,
            verbose=verbose,
        )
        return

    click.echo(f"Generating monthly summary for {month}...")

    summary = generate_monthly_summary_cc(month, verbose=verbose)

    if dry_run:
        click.echo("\n--- DRY RUN OUTPUT ---")
        click.echo(summary.to_markdown())
        return

    output_path = save_summary(summary)
    click.echo(f"Saved to {output_path}")

    if verbose:
        click.echo("\n--- Summary ---")
        click.echo(summary.to_markdown())


@cli.command()
@click.option(
    "--date",
    "date_str",
    default="yesterday",
    help="Date to process (YYYY-MM-DD, 'today', or 'yesterday')",
)
@click.pass_context
def smart(ctx: click.Context, date_str: str) -> None:
    """Smart summarization: daily + auto weekly/monthly when due.

    Weekly summaries run on Mondays. Monthly summaries run on the 1st.
    """
    verbose = ctx.obj["verbose"]
    dry_run = ctx.obj["dry_run"]
    target_date = _parse_date_arg(date_str)

    results: list[tuple[str, bool | None]] = []

    # 1. Always run daily summarization
    click.echo(f"=== Daily Summary for {target_date} ===")
    entries = get_journal_entries_for_date(target_date)
    if entries:
        try:
            summary = generate_daily_with_cc(target_date, verbose=verbose)
            if not dry_run:
                output_path = save_summary(summary)
                click.echo(f"Daily: Saved to {output_path}")
            else:
                click.echo("Daily: Would generate (dry run)")
            results.append(("daily", True))
        except Exception as e:
            click.echo(f"Daily: Failed - {e}")
            results.append(("daily", False))
    else:
        click.echo(f"Daily: No entries for {target_date}")
        results.append(("daily", None))

    # 2. Check if weekly summarization is due (Monday)
    if target_date.weekday() == 0:  # Monday
        click.echo("\n=== Weekly Summary (Monday) ===")
        last_week = target_date - timedelta(days=7)
        week = last_week.strftime("%G-W%V")
        try:
            summary = generate_weekly_summary_cc(week, verbose=verbose)
            if not dry_run:
                output_path = save_summary(summary)
                click.echo(f"Weekly: Saved to {output_path}")
            else:
                click.echo("Weekly: Would generate (dry run)")
            results.append(("weekly", True))
        except Exception as e:
            click.echo(f"Weekly: Failed - {e}")
            results.append(("weekly", False))
    else:
        click.echo("\nWeekly: Not due (not Monday)")

    # 3. Check if monthly summarization is due (1st of month)
    if target_date.day == 1:
        click.echo("\n=== Monthly Summary (1st of month) ===")
        first_of_month = target_date.replace(day=1)
        last_month = first_of_month - timedelta(days=1)
        month = last_month.strftime("%Y-%m")
        try:
            summary = generate_monthly_summary_cc(month, verbose=verbose)
            if not dry_run:
                output_path = save_summary(summary)
                click.echo(f"Monthly: Saved to {output_path}")
            else:
                click.echo("Monthly: Would generate (dry run)")
            results.append(("monthly", True))
        except Exception as e:
            click.echo(f"Monthly: Failed - {e}")
            results.append(("monthly", False))
    else:
        click.echo("\nMonthly: Not due (not 1st of month)")

    # Summary
    click.echo("\n=== Summary ===")
    for name, success in results:
        if success is True:
            click.echo(f"  {name}: OK")
        elif success is False:
            click.echo(f"  {name}: FAILED")
        else:
            click.echo(f"  {name}: skipped")


@cli.command()
@click.option("--from", "from_date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--to", "to_date", default=None, help="End date (YYYY-MM-DD, defaults to today)")
@click.option("--force", is_flag=True, help="Overwrite existing summaries")
@click.pass_context
def backfill(ctx: click.Context, from_date: str, to_date: str | None, force: bool) -> None:
    """Backfill summaries for a date range."""
    verbose = ctx.obj["verbose"]
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date) if to_date else date.today()

    click.echo(f"Backfilling daily summaries from {start} to {end}...")

    current = start
    generated = 0
    skipped = 0
    failed = 0

    while current <= end:
        entries = get_journal_entries_for_date(current)
        if entries:
            output_path = SUMMARIES_DIR / "daily" / f"{current.isoformat()}.md"
            if output_path.exists() and not force:
                if verbose:
                    click.echo(f"  Skipping {current} (already exists)")
                skipped += 1
            else:
                try:
                    summary = generate_daily_with_cc(current, verbose=verbose)
                    save_summary(summary)
                    generated += 1
                    if verbose:
                        click.echo(f"  Generated {current}")
                except Exception as e:
                    click.echo(f"  Failed {current}: {e}")
                    failed += 1
        current += timedelta(days=1)

    click.echo(f"\nBackfill complete: {generated} generated, {skipped} skipped, {failed} failed")


@cli.command()
def stats() -> None:
    """Show statistics about journal entries."""
    click.echo("Journal Statistics")
    click.echo("=" * 40)

    if not JOURNAL_DIR.exists():
        click.echo("Journal directory not found!")
        raise SystemExit(1)

    all_entries = list(JOURNAL_DIR.glob("*.md"))
    click.echo(f"Total entries: {len(all_entries)}")

    # Count by date
    import re

    dates: dict[str, int] = {}
    for entry in all_entries:
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", entry.name)
        if date_match:
            d = date_match.group(1)
            dates[d] = dates.get(d, 0) + 1

    click.echo(f"Unique dates: {len(dates)}")

    if dates:
        first_date = min(dates.keys())
        last_date = max(dates.keys())
        click.echo(f"Date range: {first_date} to {last_date}")

        # Most active days
        top_days = sorted(dates.items(), key=lambda x: -x[1])[:5]
        click.echo("\nMost active days:")
        for d, count in top_days:
            click.echo(f"  {d}: {count} entries")

    # Check existing summaries
    click.echo("\nExisting summaries:")
    for level in ["daily", "weekly", "monthly"]:
        summary_dir = SUMMARIES_DIR / level
        if summary_dir.exists():
            count = len(list(summary_dir.glob("*.md")))
            click.echo(f"  {level}: {count}")
        else:
            click.echo(f"  {level}: 0")


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
