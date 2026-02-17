"""
CLI for journal summarization.

Usage:
    summarize daily [--date DATE]
    summarize weekly [--week WEEK]
    summarize monthly [--month MONTH]
    summarize smart [--date DATE]  # Daily job that auto-runs weekly/monthly when due
    summarize backfill [--from DATE] [--to DATE]
    summarize stats

All summarization uses Claude Code backend for high-quality results.
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta

from .generator import (
    JOURNAL_DIR,
    SUMMARIES_DIR,
    WORKSPACE,
    get_journal_entries_for_date,
    save_summary,
)
from .github_data import GitHubActivity, fetch_activity, format_activity_for_prompt
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
from .cc_session_data import fetch_cc_session_stats_range
from .session_data import (
    SessionStats,
    fetch_session_stats,
    fetch_session_stats_range,
    format_sessions_for_prompt,
    merge_session_stats,
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
    """
    parts: list[str] = []

    activity_text = format_activity_for_prompt(activity)
    if activity_text:
        parts.append(activity_text)
        if verbose:
            print(
                f"  GitHub: {activity.total_commits} commits, {activity.total_prs_merged} PRs, {activity.total_issues_closed} issues"
            )

    session_text = format_sessions_for_prompt(session_stats)
    if session_text:
        parts.append(session_text)
        if verbose:
            print(
                f"  Sessions: {session_stats.session_count}, tokens: {session_stats.total_tokens:,}"
            )

    # Fetch workspace activity (tweets, emails)
    ws_activity = fetch_workspace_activity(start, end, WORKSPACE)
    ws_text = format_workspace_activity_for_prompt(ws_activity)
    if ws_text:
        parts.append(ws_text)
        if verbose:
            print(
                f"  Workspace: {len(ws_activity.tweets)} tweets, {len(ws_activity.emails)} emails"
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
        print("Fetching real data sources...")
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


def cmd_daily(args):
    """Generate daily summary."""
    if args.date == "today":
        target_date = date.today()
    elif args.date == "yesterday":
        target_date = date.today() - timedelta(days=1)
    else:
        target_date = date.fromisoformat(args.date)

    entries = get_journal_entries_for_date(target_date)
    if not entries:
        print(f"No journal entries found for {target_date}")
        return 1

    print(f"Generating daily summary for {target_date} ({len(entries)} entries)...")

    summary = generate_daily_with_cc(target_date, verbose=args.verbose)

    if args.dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(summary.to_markdown())
        return 0

    output_path = save_summary(summary)
    print(f"Saved to {output_path}")

    if args.verbose:
        print("\n--- Summary ---")
        print(summary.to_markdown())

    return 0


def generate_weekly_summary_cc(week: str, verbose: bool = False):
    """Generate weekly summary using Claude Code backend."""
    from .cc_backend import summarize_weekly_with_cc
    from .schemas import WeeklySummary, Metrics, Decision

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
        print("Fetching real data sources...")
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


def cmd_weekly(args):
    """Generate weekly summary."""
    if args.week == "current":
        today = date.today()
        week = today.strftime("%G-W%V")
    elif args.week == "last":
        last_week = date.today() - timedelta(days=7)
        week = last_week.strftime("%G-W%V")
    else:
        week = args.week

    print(f"Generating weekly summary for {week}...")

    summary = generate_weekly_summary_cc(week, verbose=args.verbose)

    if args.dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(summary.to_markdown())
        return 0

    output_path = save_summary(summary)
    print(f"Saved to {output_path}")

    if args.verbose:
        print("\n--- Summary ---")
        print(summary.to_markdown())

    return 0


def generate_monthly_summary_cc(month: str, verbose: bool = False):
    """Generate monthly summary using Claude Code backend."""
    from .cc_backend import summarize_monthly_with_cc
    from .schemas import Metrics, Decision

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
        print("Fetching real data sources...")
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


def cmd_monthly(args):
    """Generate monthly summary."""
    if args.month == "current":
        month = date.today().strftime("%Y-%m")
    elif args.month == "last":
        first_of_month = date.today().replace(day=1)
        last_month = first_of_month - timedelta(days=1)
        month = last_month.strftime("%Y-%m")
    else:
        month = args.month

    print(f"Generating monthly summary for {month}...")

    summary = generate_monthly_summary_cc(month, verbose=args.verbose)

    if args.dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(summary.to_markdown())
        return 0

    output_path = save_summary(summary)
    print(f"Saved to {output_path}")

    if args.verbose:
        print("\n--- Summary ---")
        print(summary.to_markdown())

    return 0


def cmd_smart(args):
    """
    Smart summarization: runs daily, and automatically runs weekly/monthly when due.

    Weekly: Run on Mondays
    Monthly: Run on the 1st of each month
    """
    if args.date == "today":
        target_date = date.today()
    elif args.date == "yesterday":
        target_date = date.today() - timedelta(days=1)
    else:
        target_date = date.fromisoformat(args.date)

    results: list[tuple[str, bool | None]] = []

    # 1. Always run daily summarization
    print(f"=== Daily Summary for {target_date} ===")
    entries = get_journal_entries_for_date(target_date)
    if entries:
        try:
            summary = generate_daily_with_cc(target_date, verbose=args.verbose)
            if not args.dry_run:
                output_path = save_summary(summary)
                print(f"Daily: Saved to {output_path}")
            else:
                print("Daily: Would generate (dry run)")
            results.append(("daily", True))
        except Exception as e:
            print(f"Daily: Failed - {e}")
            results.append(("daily", False))
    else:
        print(f"Daily: No entries for {target_date}")
        results.append(("daily", None))

    # 2. Check if weekly summarization is due (Monday)
    if target_date.weekday() == 0:  # Monday
        print("\n=== Weekly Summary (Monday) ===")
        last_week = target_date - timedelta(days=7)
        week = last_week.strftime("%G-W%V")
        try:
            summary = generate_weekly_summary_cc(week, verbose=args.verbose)
            if not args.dry_run:
                output_path = save_summary(summary)
                print(f"Weekly: Saved to {output_path}")
            else:
                print("Weekly: Would generate (dry run)")
            results.append(("weekly", True))
        except Exception as e:
            print(f"Weekly: Failed - {e}")
            results.append(("weekly", False))
    else:
        print("\nWeekly: Not due (not Monday)")

    # 3. Check if monthly summarization is due (1st of month)
    if target_date.day == 1:
        print("\n=== Monthly Summary (1st of month) ===")
        first_of_month = target_date.replace(day=1)
        last_month = first_of_month - timedelta(days=1)
        month = last_month.strftime("%Y-%m")
        try:
            summary = generate_monthly_summary_cc(month, verbose=args.verbose)
            if not args.dry_run:
                output_path = save_summary(summary)
                print(f"Monthly: Saved to {output_path}")
            else:
                print("Monthly: Would generate (dry run)")
            results.append(("monthly", True))
        except Exception as e:
            print(f"Monthly: Failed - {e}")
            results.append(("monthly", False))
    else:
        print("\nMonthly: Not due (not 1st of month)")

    # Summary
    print("\n=== Summary ===")
    for name, success in results:
        if success is True:
            print(f"  {name}: OK")
        elif success is False:
            print(f"  {name}: FAILED")
        else:
            print(f"  {name}: skipped")

    return 0


def cmd_backfill(args):
    """Backfill summaries for a date range."""
    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date) if args.to_date else date.today()

    print(f"Backfilling daily summaries from {from_date} to {to_date}...")

    current = from_date
    generated = 0
    skipped = 0
    failed = 0

    while current <= to_date:
        entries = get_journal_entries_for_date(current)
        if entries:
            output_path = SUMMARIES_DIR / "daily" / f"{current.isoformat()}.md"
            if output_path.exists() and not args.force:
                if args.verbose:
                    print(f"  Skipping {current} (already exists)")
                skipped += 1
            else:
                try:
                    summary = generate_daily_with_cc(current, verbose=args.verbose)
                    save_summary(summary)
                    generated += 1
                    if args.verbose:
                        print(f"  Generated {current}")
                except Exception as e:
                    print(f"  Failed {current}: {e}")
                    failed += 1
        current += timedelta(days=1)

    print(f"\nBackfill complete: {generated} generated, {skipped} skipped, {failed} failed")
    return 0


def cmd_stats(args):
    """Show statistics about journal entries."""
    print("Journal Statistics")
    print("=" * 40)

    if not JOURNAL_DIR.exists():
        print("Journal directory not found!")
        return 1

    all_entries = list(JOURNAL_DIR.glob("*.md"))
    print(f"Total entries: {len(all_entries)}")

    # Count by date
    dates: dict[str, int] = {}
    for entry in all_entries:
        import re

        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", entry.name)
        if date_match:
            d = date_match.group(1)
            dates[d] = dates.get(d, 0) + 1

    print(f"Unique dates: {len(dates)}")

    if dates:
        first_date = min(dates.keys())
        last_date = max(dates.keys())
        print(f"Date range: {first_date} to {last_date}")

        # Most active days
        top_days = sorted(dates.items(), key=lambda x: -x[1])[:5]
        print("\nMost active days:")
        for d, count in top_days:
            print(f"  {d}: {count} entries")

    # Check existing summaries
    print("\nExisting summaries:")
    for level in ["daily", "weekly", "monthly"]:
        summary_dir = SUMMARIES_DIR / level
        if summary_dir.exists():
            count = len(list(summary_dir.glob("*.md")))
            print(f"  {level}: {count}")
        else:
            print(f"  {level}: 0")

    return 0


def cmd_github(args):
    """Generate activity summary for a GitHub user (human mode)."""
    from .cc_backend import summarize_github_activity_with_cc
    from .github_data import fetch_user_activity, format_activity_for_prompt

    username = args.user

    # Parse period
    if args.period == "daily":
        if args.date == "today":
            target_date = date.today()
        elif args.date == "yesterday":
            target_date = date.today() - timedelta(days=1)
        else:
            target_date = date.fromisoformat(args.date)
        start = target_date
        end = target_date
        period_str = target_date.isoformat()
    elif args.period == "weekly":
        end = date.today() if args.date == "today" else date.today() - timedelta(days=1)
        start = end - timedelta(days=6)
        period_str = f"{start.isoformat()} to {end.isoformat()}"
    elif args.period == "monthly":
        end = date.today() if args.date == "today" else date.today() - timedelta(days=1)
        start = end.replace(day=1)
        period_str = f"{start.isoformat()} to {end.isoformat()}"
    else:
        print(f"Unknown period: {args.period}")
        return 1

    print(f"Fetching GitHub activity for @{username} ({period_str})...")

    # Fetch GitHub activity for this user
    activity = fetch_user_activity(start, end, username)

    if args.verbose:
        print(
            f"  Found: {activity.total_commits} commits, "
            f"{activity.total_prs_merged} PRs, "
            f"{activity.total_issues_closed} issues, "
            f"{len(activity.repos)} repos"
        )

    github_text = format_activity_for_prompt(activity)

    if not github_text:
        print(f"No GitHub activity found for @{username} in this period.")
        return 0

    if args.raw:
        # Just print the raw GitHub data without LLM summarization
        print(f"\n{github_text}")
        return 0

    print("Generating summary with Claude Code...")
    result = summarize_github_activity_with_cc(github_text, username, period_str)

    # Format output
    print(f"\n## GitHub Activity Summary: @{username}")
    print(f"**Period**: {period_str}\n")

    if result.get("narrative"):
        print(result["narrative"])
        print()

    if result.get("highlights"):
        print("### Highlights")
        for h in result["highlights"]:
            print(f"- {h}")
        print()

    if result.get("projects_active"):
        print("### Active Projects")
        for p in result["projects_active"]:
            desc = p.get("description", "")
            repo = p.get("repo", "")
            prs = p.get("prs", 0)
            issues = p.get("issues", 0)
            print(f"- **{repo}**: {desc} ({prs} PRs, {issues} issues)")
        print()

    if result.get("themes"):
        print("### Themes")
        for t in result["themes"]:
            print(f"- {t}")
        print()

    stats = result.get("stats", {})
    if stats:
        print("### Stats")
        print(f"- Commits: {stats.get('total_commits', 0)}")
        print(f"- PRs: {stats.get('total_prs', 0)}")
        print(f"- Issues: {stats.get('total_issues', 0)}")
        print(f"- Repos active: {stats.get('repos_active', 0)}")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Activity summarization — journals, GitHub, sessions, tweets, email",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--dry-run", action="store_true", help="Print output without saving")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # daily command
    daily_parser = subparsers.add_parser("daily", help="Generate daily summary")
    daily_parser.add_argument(
        "--date",
        default="yesterday",
        help="Date to summarize (YYYY-MM-DD, 'today', or 'yesterday')",
    )

    # weekly command
    weekly_parser = subparsers.add_parser("weekly", help="Generate weekly summary")
    weekly_parser.add_argument(
        "--week",
        default="last",
        help="Week to summarize (YYYY-Www, 'current', or 'last')",
    )

    # monthly command
    monthly_parser = subparsers.add_parser("monthly", help="Generate monthly summary")
    monthly_parser.add_argument(
        "--month",
        default="last",
        help="Month to summarize (YYYY-MM, 'current', or 'last')",
    )

    # smart command (new)
    smart_parser = subparsers.add_parser(
        "smart", help="Smart summarization: daily + auto weekly/monthly when due"
    )
    smart_parser.add_argument(
        "--date",
        default="yesterday",
        help="Date to process (YYYY-MM-DD, 'today', or 'yesterday')",
    )

    # backfill command
    backfill_parser = subparsers.add_parser("backfill", help="Backfill summaries")
    backfill_parser.add_argument(
        "--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)"
    )
    backfill_parser.add_argument(
        "--to", dest="to_date", help="End date (YYYY-MM-DD, defaults to today)"
    )
    backfill_parser.add_argument(
        "--force", action="store_true", help="Overwrite existing summaries"
    )

    # stats command
    subparsers.add_parser("stats", help="Show journal statistics")

    # github command (human mode)
    github_parser = subparsers.add_parser(
        "github",
        help="Summarize GitHub activity for any user (no journal needed)",
    )
    github_parser.add_argument(
        "--user",
        required=True,
        help="GitHub username to summarize activity for",
    )
    github_parser.add_argument(
        "--period",
        default="weekly",
        choices=["daily", "weekly", "monthly"],
        help="Time period to summarize (default: weekly)",
    )
    github_parser.add_argument(
        "--date",
        default="today",
        help="Reference date (YYYY-MM-DD or 'today'/'yesterday'). Period is calculated relative to this.",
    )
    github_parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw GitHub data without LLM summarization",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "daily": cmd_daily,
        "weekly": cmd_weekly,
        "monthly": cmd_monthly,
        "smart": cmd_smart,
        "backfill": cmd_backfill,
        "stats": cmd_stats,
        "github": cmd_github,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
