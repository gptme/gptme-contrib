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
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .generator import (
    JOURNAL_DIR,
    SUMMARIES_DIR,
    get_journal_entries_for_date,
    save_summary,
)
from .schemas import (
    Blocker,
    BlockerStatus,
    DailySummary,
    Decision,
    Metrics,
    MonthlySummary,
)


def generate_daily_with_cc(target_date: date) -> DailySummary:
    """Generate daily summary using Claude Code backend."""
    from .cc_backend import summarize_daily_with_cc

    entries = get_journal_entries_for_date(target_date)
    if not entries:
        raise ValueError(f"No entries for {target_date}")

    # Load content for each entry
    entry_contents = [(p, p.read_text()) for p in entries]

    # Get summary from Claude Code
    result = summarize_daily_with_cc(entry_contents, str(target_date))

    # Convert to DailySummary schema
    return DailySummary(
        date=target_date,
        session_count=len(entries),
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
                status=BlockerStatus(b.get("status", "active")),
            )
            for b in result.get("blockers", [])
        ],
        themes=result.get("themes", []),
        work_in_progress=result.get("work_in_progress", [])[:5],
        metrics=Metrics(
            commits=result.get("metrics", {}).get("commits", 0),
            prs_created=result.get("metrics", {}).get("prs_created", 0),
            prs_merged=result.get("metrics", {}).get("prs_merged", 0),
            issues_created=result.get("metrics", {}).get("issues_created", 0),
            issues_closed=result.get("metrics", {}).get("issues_closed", 0),
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

    summary = generate_daily_with_cc(target_date)

    if args.dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(summary.to_markdown())
        return 0

    output_path = save_summary(summary)
    print(f"✅ Saved to {output_path}")

    if args.verbose:
        print("\n--- Summary ---")
        print(summary.to_markdown())

    return 0


def generate_weekly_summary_cc(week: str):
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

    # Count journal sessions for the week
    total_sessions = 0
    current = start_date
    while current <= end_date:
        date_str = current.isoformat()
        # Count old format: journal/YYYY-MM-DD.md and journal/YYYY-MM-DD-*.md
        old_format = list(JOURNAL_DIR.glob(f"{date_str}*.md"))
        # Count new format: journal/YYYY-MM-DD/*.md (subdirectory)
        new_format_dir = JOURNAL_DIR / date_str
        new_format = list(new_format_dir.glob("*.md")) if new_format_dir.exists() else []
        total_sessions += len(old_format) + len(new_format)
        current += timedelta(days=1)

    # Load daily summaries as dicts
    daily_summaries = []
    current_date = start_date
    while current_date <= end_date:
        daily_path = Path("knowledge/summaries/daily") / f"{current_date.isoformat()}.md"
        if daily_path.exists():
            # Parse basic info from markdown for CC context
            content = daily_path.read_text()
            daily_summaries.append(
                {
                    "date": current_date.isoformat(),
                    "accomplishments": extract_list_from_md(content, "Accomplishments"),
                    "themes": extract_list_from_md(content, "Themes"),
                    "key_insight": extract_single_from_md(content, "Key Insight"),
                }
            )
        current_date += timedelta(days=1)

    if not daily_summaries:
        raise ValueError(f"No daily summaries found for {week}")

    # Call Claude Code backend
    cc_result = summarize_weekly_with_cc(daily_summaries, week)

    # Convert to WeeklySummary schema
    decisions = []
    for d in cc_result.get("key_decisions", []):
        if isinstance(d, dict):
            decisions.append(
                Decision(
                    topic=d.get("topic", ""),
                    decision=d.get("decision", ""),
                    rationale=d.get("impact", d.get("rationale", "")),
                )
            )

    metrics_data = cc_result.get("metrics", {})

    return WeeklySummary(
        week=week,
        start_date=start_date,
        end_date=end_date,
        milestones=cc_result.get("top_accomplishments", []),
        recurring_themes=cc_result.get("themes", []),
        key_decisions=decisions,
        trends=cc_result.get("patterns", []),
        metrics=Metrics(
            sessions=total_sessions,
            commits=metrics_data.get("commits", 0),
            prs_merged=metrics_data.get("prs_merged", 0),
            issues_closed=metrics_data.get("issues_closed", 0),
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

    summary = generate_weekly_summary_cc(week)

    if args.dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(summary.to_markdown())
        return 0

    output_path = save_summary(summary)
    print(f"✅ Saved to {output_path}")

    if args.verbose:
        print("\n--- Summary ---")
        print(summary.to_markdown())

    return 0


def generate_monthly_summary_cc(month: str):
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

    # Load weekly summaries as dicts and aggregate metrics
    weekly_summaries = []
    total_commits = 0
    total_prs = 0
    total_sessions = 0
    current = first_day
    weeks_processed = set()
    while current <= last_day:
        week = current.strftime("%G-W%V")
        if week not in weeks_processed:
            weeks_processed.add(week)
            weekly_path = Path("knowledge/summaries/weekly") / f"{week}.md"
            if weekly_path.exists():
                content = weekly_path.read_text()
                # Extract milestones (try both "Major Milestones" and "Milestones")
                milestones = extract_list_from_md(content, "Major Milestones")
                if not milestones:
                    milestones = extract_list_from_md(content, "Milestones")
                weekly_summaries.append(
                    {
                        "week": week,
                        "milestones": milestones,
                        "top_accomplishments": milestones,
                        "recurring_themes": extract_list_from_md(content, "Recurring Themes"),
                        "themes": extract_list_from_md(content, "Recurring Themes"),
                        "weekly_insight": "",  # May not exist in older summaries
                    }
                )
                # Extract metrics from header line: **Sessions**: X | **Commits**: Y | **PRs Merged**: Z
                import re

                metrics_match = re.search(
                    r"\*\*Sessions\*\*:\s*(\d+).*\*\*Commits\*\*:\s*(\d+).*\*\*PRs Merged\*\*:\s*(\d+)",
                    content,
                )
                if metrics_match:
                    total_sessions += int(metrics_match.group(1))
                    total_commits += int(metrics_match.group(2))
                    total_prs += int(metrics_match.group(3))
        current += timedelta(days=1)

    if not weekly_summaries:
        raise ValueError(f"No weekly summaries found for {month}")

    # Call Claude Code backend
    cc_result = summarize_monthly_with_cc(weekly_summaries, month)

    # Convert to MonthlySummary schema
    decisions = []
    for d in cc_result.get("strategic_decisions", []):
        if isinstance(d, dict):
            decisions.append(
                Decision(
                    topic=d.get("topic", ""),
                    decision=d.get("decision", ""),
                    rationale=d.get("strategic_impact", d.get("rationale", "")),
                )
            )

    return MonthlySummary(
        month=month,
        accomplishments=cc_result.get("major_achievements", []),
        key_learnings=cc_result.get("key_learnings", []),
        strategic_decisions=decisions,
        metrics=Metrics(
            sessions=total_sessions,
            commits=total_commits,
            prs_merged=total_prs,
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

    summary = generate_monthly_summary_cc(month)

    if args.dry_run:
        print("\n--- DRY RUN OUTPUT ---")
        print(summary.to_markdown())
        return 0

    output_path = save_summary(summary)
    print(f"✅ Saved to {output_path}")

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
            summary = generate_daily_with_cc(target_date)
            if not args.dry_run:
                output_path = save_summary(summary)
                print(f"✅ Daily: Saved to {output_path}")
            else:
                print("✅ Daily: Would generate (dry run)")
            results.append(("daily", True))
        except Exception as e:
            print(f"❌ Daily: Failed - {e}")
            results.append(("daily", False))
    else:
        print(f"⚠️ Daily: No entries for {target_date}")
        results.append(("daily", None))

    # 2. Check if weekly summarization is due (Monday)
    if target_date.weekday() == 0:  # Monday
        print("\n=== Weekly Summary (Monday) ===")
        last_week = target_date - timedelta(days=7)
        week = last_week.strftime("%G-W%V")
        try:
            summary = generate_weekly_summary_cc(week)
            if not args.dry_run:
                output_path = save_summary(summary)
                print(f"✅ Weekly: Saved to {output_path}")
            else:
                print("✅ Weekly: Would generate (dry run)")
            results.append(("weekly", True))
        except Exception as e:
            print(f"❌ Weekly: Failed - {e}")
            results.append(("weekly", False))
    else:
        print("\n⏭️ Weekly: Not due (not Monday)")

    # 3. Check if monthly summarization is due (1st of month)
    if target_date.day == 1:
        print("\n=== Monthly Summary (1st of month) ===")
        first_of_month = target_date.replace(day=1)
        last_month = first_of_month - timedelta(days=1)
        month = last_month.strftime("%Y-%m")
        try:
            summary = generate_monthly_summary_cc(month)
            if not args.dry_run:
                output_path = save_summary(summary)
                print(f"✅ Monthly: Saved to {output_path}")
            else:
                print("✅ Monthly: Would generate (dry run)")
            results.append(("monthly", True))
        except Exception as e:
            print(f"❌ Monthly: Failed - {e}")
            results.append(("monthly", False))
    else:
        print("\n⏭️ Monthly: Not due (not 1st of month)")

    # Summary
    print("\n=== Summary ===")
    for name, success in results:
        if success is True:
            print(f"  ✅ {name}")
        elif success is False:
            print(f"  ❌ {name}")
        else:
            print(f"  ⏭️ {name} (skipped)")

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
                    summary = generate_daily_with_cc(current)
                    save_summary(summary)
                    generated += 1
                    if args.verbose:
                        print(f"  ✅ Generated {current}")
                except Exception as e:
                    print(f"  ❌ Failed {current}: {e}")
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


def main():
    parser = argparse.ArgumentParser(
        description="Recursive journal summarization system (Claude Code backend)",
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
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
