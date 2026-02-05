#!/usr/bin/env python3
"""ACE Visualization CLI

Command-line interface for exploring ACE data:
- Delta statuses and details
- Curation quality metrics
- Insight quality summaries
- Trend analysis over time

Part of gptme-ace plugin (Phase 5: Utilities).
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import click

from .curator import Delta, DeltaOperation
from .metrics import (
    CurationRun,
    InsightQuality,
    LessonImpact,
    get_default_metrics_db,
)
from .storage import InsightStorage


def _format_datetime(dt: datetime | str | None) -> str:
    """Format datetime for display"""
    if dt is None:
        return "-"
    if isinstance(dt, str):
        return dt[:19]  # Trim to seconds
    return dt.strftime("%Y-%m-%d %H:%M")


def _load_deltas(delta_dir: Path) -> list[Delta]:
    """Load all deltas from directory"""
    deltas = []
    if not delta_dir.exists():
        return deltas

    for delta_file in delta_dir.glob("*.json"):
        try:
            with open(delta_file) as f:
                data = json.load(f)
                # Convert operations to DeltaOperation objects
                operations = [
                    DeltaOperation(
                        type=op["type"],
                        section=op["section"],
                        content=op.get("content"),
                        position=op.get("position"),
                        target=op.get("target"),
                    )
                    for op in data.get("operations", [])
                ]
                delta = Delta(
                    delta_id=data["delta_id"],
                    created=data["created"],
                    source=data["source"],
                    source_insights=data.get("source_insights", []),
                    lesson_id=data["lesson_id"],
                    operations=operations,
                    rationale=data["rationale"],
                    review_status=data.get("review_status", "pending"),
                    applied_at=data.get("applied_at"),
                    applied_by=data.get("applied_by"),
                )
                deltas.append(delta)
        except (json.JSONDecodeError, KeyError) as e:
            click.echo(f"Warning: Failed to load {delta_file}: {e}", err=True)

    return sorted(deltas, key=lambda d: d.created, reverse=True)


@click.group()
@click.option(
    "--data-dir",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Data directory for metrics and deltas (default: workspace-relative)",
)
@click.pass_context
def cli(ctx: click.Context, data_dir: Optional[Path]):
    """ACE Visualization CLI - Explore context optimization data"""
    ctx.ensure_object(dict)

    # Determine data directory
    if data_dir is None:
        # Try common locations
        for candidate in [Path("deltas"), Path.cwd() / "deltas"]:
            if candidate.exists():
                data_dir = candidate.parent
                break
        if data_dir is None:
            data_dir = Path.cwd()

    ctx.obj["data_dir"] = data_dir
    ctx.obj["delta_dir"] = data_dir / "deltas"
    ctx.obj["metrics_db"] = get_default_metrics_db()


# ============================================================================
# Delta Commands
# ============================================================================


@cli.group()
def deltas():
    """Manage and view deltas (lesson update proposals)"""
    pass


@deltas.command("list")
@click.option(
    "--status",
    type=click.Choice(["all", "pending", "approved", "rejected"]),
    default="all",
    help="Filter by review status",
)
@click.option("--limit", "-n", default=20, help="Maximum number of deltas to show")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def deltas_list(ctx: click.Context, status: str, limit: int, json_output: bool) -> None:
    """List deltas with their review status"""
    delta_dir = ctx.obj["delta_dir"]
    deltas = _load_deltas(delta_dir)

    # Filter by status
    if status != "all":
        deltas = [d for d in deltas if d.review_status == status]

    # Apply limit
    deltas = deltas[:limit]

    if json_output:
        output = [
            {
                "delta_id": d.delta_id,
                "created": d.created,
                "lesson_id": d.lesson_id,
                "status": d.review_status,
                "operations": len(d.operations),
            }
            for d in deltas
        ]
        click.echo(json.dumps(output, indent=2))
        return

    # Human-readable output
    if not deltas:
        click.echo(f"No deltas found with status: {status}")
        return

    click.echo(f"\n📋 Deltas ({len(deltas)} shown)")
    click.echo("=" * 80)

    status_icons = {"pending": "⏳", "approved": "✅", "rejected": "❌"}

    for delta in deltas:
        icon = status_icons.get(delta.review_status, "❓")
        ops_summary = ", ".join(
            f"{op.type}:{op.section[:20]}" for op in delta.operations[:3]
        )
        if len(delta.operations) > 3:
            ops_summary += f" +{len(delta.operations) - 3} more"

        click.echo(f"\n{icon} {delta.delta_id[:12]}... | {delta.lesson_id[:30]}")
        click.echo(f"   Created: {_format_datetime(delta.created)}")
        click.echo(f"   Operations: {ops_summary}")
        if delta.applied_at:
            click.echo(f"   Applied: {_format_datetime(delta.applied_at)}")


@deltas.command("show")
@click.argument("delta_id")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def deltas_show(ctx: click.Context, delta_id: str, json_output: bool) -> None:
    """Show detailed information about a specific delta"""
    delta_dir = ctx.obj["delta_dir"]
    deltas = _load_deltas(delta_dir)

    # Find matching delta (supports partial ID)
    matches = [d for d in deltas if d.delta_id.startswith(delta_id)]

    if not matches:
        click.echo(f"Error: No delta found matching '{delta_id}'", err=True)
        raise SystemExit(1)

    if len(matches) > 1:
        click.echo(f"Error: Multiple deltas match '{delta_id}':", err=True)
        for m in matches:
            click.echo(f"  - {m.delta_id}")
        raise SystemExit(1)

    delta = matches[0]

    if json_output:
        output = {
            "delta_id": delta.delta_id,
            "created": delta.created,
            "source": delta.source,
            "source_insights": delta.source_insights,
            "lesson_id": delta.lesson_id,
            "operations": [
                {
                    "type": op.type,
                    "section": op.section,
                    "content": op.content,
                    "position": op.position,
                    "target": op.target,
                }
                for op in delta.operations
            ],
            "rationale": delta.rationale,
            "review_status": delta.review_status,
            "applied_at": delta.applied_at,
            "applied_by": delta.applied_by,
        }
        click.echo(json.dumps(output, indent=2))
        return

    # Human-readable output
    status_icons = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    icon = status_icons.get(delta.review_status, "❓")

    click.echo(f"\n{icon} Delta: {delta.delta_id}")
    click.echo("=" * 80)
    click.echo(f"Lesson: {delta.lesson_id}")
    click.echo(f"Created: {_format_datetime(delta.created)}")
    click.echo(f"Status: {delta.review_status}")
    click.echo(f"Source: {delta.source}")

    if delta.source_insights:
        click.echo(f"Source Insights: {', '.join(delta.source_insights[:5])}")
        if len(delta.source_insights) > 5:
            click.echo(f"  ... and {len(delta.source_insights) - 5} more")

    if delta.applied_at:
        click.echo(f"Applied: {_format_datetime(delta.applied_at)}")
        click.echo(f"Applied By: {delta.applied_by or 'unknown'}")

    click.echo("\n📝 Rationale:")
    click.echo(f"  {delta.rationale}")

    click.echo(f"\n🔧 Operations ({len(delta.operations)}):")
    for i, op in enumerate(delta.operations, 1):
        click.echo(f"\n  {i}. {op.type.upper()} → {op.section}")
        if op.content:
            preview = op.content[:200]
            if len(op.content) > 200:
                preview += "..."
            click.echo(f"     Content: {preview}")
        if op.position:
            click.echo(f"     Position: {op.position}")
        if op.target:
            click.echo(f"     Target: {op.target}")


@deltas.command("summary")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def deltas_summary(ctx: click.Context, json_output: bool) -> None:
    """Show summary statistics for deltas"""
    delta_dir = ctx.obj["delta_dir"]
    deltas = _load_deltas(delta_dir)

    # Count by status
    by_status = {"pending": 0, "approved": 0, "rejected": 0}
    by_lesson: dict[str, int] = {}
    by_operation: dict[str, int] = {}

    for delta in deltas:
        status = delta.review_status
        if status in by_status:
            by_status[status] += 1
        else:
            by_status[status] = 1

        by_lesson[delta.lesson_id] = by_lesson.get(delta.lesson_id, 0) + 1

        for op in delta.operations:
            by_operation[op.type] = by_operation.get(op.type, 0) + 1

    if json_output:
        click.echo(
            json.dumps(
                {
                    "total": len(deltas),
                    "by_status": by_status,
                    "by_lesson": by_lesson,
                    "by_operation": by_operation,
                },
                indent=2,
            )
        )
        return

    click.echo("\n📊 Delta Summary")
    click.echo("=" * 60)
    click.echo(f"Total Deltas: {len(deltas)}")

    click.echo("\n📋 By Status:")
    click.echo(f"  ⏳ Pending:  {by_status.get('pending', 0)}")
    click.echo(f"  ✅ Approved: {by_status.get('approved', 0)}")
    click.echo(f"  ❌ Rejected: {by_status.get('rejected', 0)}")

    if by_operation:
        click.echo("\n🔧 By Operation Type:")
        for op_type, count in sorted(by_operation.items(), key=lambda x: -x[1]):
            click.echo(f"  {op_type}: {count}")

    if by_lesson:
        click.echo("\n📚 Top Lessons (by delta count):")
        top_lessons = sorted(by_lesson.items(), key=lambda x: -x[1])[:5]
        for lesson, count in top_lessons:
            click.echo(f"  {lesson[:50]}: {count}")


# ============================================================================
# Metrics Commands
# ============================================================================


@cli.group()
def metrics():
    """View curation quality metrics"""
    pass


@metrics.command("runs")
@click.option("--days", "-d", default=7, help="Number of days to show")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def metrics_runs(ctx: click.Context, days: int, json_output: bool) -> None:
    """Show recent curation runs"""
    db = ctx.obj["metrics_db"]
    if db is None:
        click.echo("Error: Metrics database not available", err=True)
        raise SystemExit(1)

    since = datetime.now() - timedelta(days=days)
    runs = db.get_curation_runs(since=since)

    if json_output:
        output = [
            {
                "run_id": r.run_id,
                "timestamp": r.timestamp.isoformat(),
                "trigger": r.trigger,
                "insights_count": r.insights_count,
                "conversions": r.conversions,
                "duration_seconds": r.duration_seconds,
                "tokens_used": r.tokens_used,
                "success": r.success,
                "error_message": r.error_message,
            }
            for r in runs
        ]
        click.echo(json.dumps(output, indent=2))
        return

    if not runs:
        click.echo(f"No curation runs in the last {days} days")
        return

    click.echo(f"\n🏃 Curation Runs (last {days} days)")
    click.echo("=" * 80)

    total_insights = sum(r.insights_count for r in runs)
    total_conversions = sum(r.conversions for r in runs)
    total_tokens = sum(r.tokens_used for r in runs)
    successful = sum(1 for r in runs if r.success)

    click.echo(f"Total Runs: {len(runs)} ({successful} successful)")
    click.echo(f"Total Insights: {total_insights}")
    click.echo(f"Total Conversions: {total_conversions}")
    if total_insights > 0:
        rate = total_conversions / total_insights * 100
        click.echo(f"Conversion Rate: {rate:.1f}%")
    click.echo(f"Total Tokens: {total_tokens:,}")

    click.echo("\n📋 Recent Runs:")
    for run in runs[:10]:
        status = "✅" if run.success else "❌"
        click.echo(
            f"  {status} {_format_datetime(run.timestamp)} | "
            f"{run.trigger:15} | "
            f"insights:{run.insights_count} → conversions:{run.conversions} | "
            f"{run.duration_seconds:.1f}s"
        )
        if run.error_message:
            click.echo(f"     Error: {run.error_message}")


@metrics.command("quality")
@click.option("--days", "-d", default=7, help="Number of days to analyze")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def metrics_quality(ctx: click.Context, days: int, json_output: bool) -> None:
    """Show insight quality statistics"""
    db = ctx.obj["metrics_db"]
    if db is None:
        click.echo("Error: Metrics database not available", err=True)
        raise SystemExit(1)

    since = datetime.now() - timedelta(days=days)
    insights = db.get_insights(since=since)

    if json_output:
        output = {
            "period_days": days,
            "total_insights": len(insights),
            "avg_quality": (
                sum(i.quality_score for i in insights) / len(insights)
                if insights
                else 0
            ),
            "actionable_count": sum(1 for i in insights if i.actionable),
            "novel_count": sum(1 for i in insights if i.novel),
            "by_category": {},
        }
        for insight in insights:
            cat = insight.category
            if cat not in output["by_category"]:
                output["by_category"][cat] = {"count": 0, "avg_quality": 0}
            output["by_category"][cat]["count"] += 1
        click.echo(json.dumps(output, indent=2))
        return

    if not insights:
        click.echo(f"No insights recorded in the last {days} days")
        return

    click.echo(f"\n⭐ Insight Quality (last {days} days)")
    click.echo("=" * 60)

    avg_quality = sum(i.quality_score for i in insights) / len(insights)
    actionable_count = sum(1 for i in insights if i.actionable)
    novel_count = sum(1 for i in insights if i.novel)

    click.echo(f"Total Insights: {len(insights)}")
    click.echo(f"Average Quality: {avg_quality:.2f}")
    click.echo(
        f"Actionable: {actionable_count} ({actionable_count/len(insights)*100:.1f}%)"
    )
    click.echo(f"Novel: {novel_count} ({novel_count/len(insights)*100:.1f}%)")

    # By category
    by_category: dict[str, list[InsightQuality]] = {}
    for insight in insights:
        cat = insight.category
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(insight)

    if by_category:
        click.echo("\n📂 By Category:")
        for cat, cat_insights in sorted(by_category.items(), key=lambda x: -len(x[1])):
            cat_avg = sum(i.quality_score for i in cat_insights) / len(cat_insights)
            click.echo(f"  {cat}: {len(cat_insights)} (avg: {cat_avg:.2f})")


@metrics.command("impact")
@click.option("--top", "-n", default=10, help="Number of top lessons to show")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def metrics_impact(ctx: click.Context, top: int, json_output: bool) -> None:
    """Show lesson impact statistics"""
    db = ctx.obj["metrics_db"]
    if db is None:
        click.echo("Error: Metrics database not available", err=True)
        raise SystemExit(1)

    lessons = db.get_lessons()

    # Sort by helpful ratio (helpful / total_uses)
    def impact_score(lesson: LessonImpact) -> float:
        if lesson.total_uses == 0:
            return 0
        return lesson.helpful_count / lesson.total_uses

    lessons = sorted(lessons, key=impact_score, reverse=True)[:top]

    if json_output:
        output = [
            {
                "lesson_id": lesson.lesson_id,
                "created": lesson.created_timestamp.isoformat(),
                "total_uses": lesson.total_uses,
                "helpful_count": lesson.helpful_count,
                "harmful_count": lesson.harmful_count,
                "impact_score": impact_score(lesson),
            }
            for lesson in lessons
        ]
        click.echo(json.dumps(output, indent=2))
        return

    if not lessons:
        click.echo("No lesson impact data available")
        return

    click.echo(f"\n📈 Lesson Impact (top {top})")
    click.echo("=" * 80)

    for i, lesson in enumerate(lessons, 1):
        score = impact_score(lesson)
        bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
        click.echo(f"\n{i}. {lesson.lesson_id[:50]}")
        click.echo(
            f"   Uses: {lesson.total_uses} | "
            f"Helpful: {lesson.helpful_count} | "
            f"Harmful: {lesson.harmful_count}"
        )
        click.echo(f"   Impact: [{bar}] {score:.1%}")


@metrics.command("trends")
@click.option("--days", "-d", default=30, help="Number of days to analyze")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def metrics_trends(ctx: click.Context, days: int, json_output: bool) -> None:
    """Show trends over time"""
    db = ctx.obj["metrics_db"]
    if db is None:
        click.echo("Error: Metrics database not available", err=True)
        raise SystemExit(1)

    since = datetime.now() - timedelta(days=days)
    runs = db.get_curation_runs(since=since)
    insights = db.get_insights(since=since)

    # Group by week
    runs_by_week: dict[str, list[CurationRun]] = {}
    insights_by_week: dict[str, list[InsightQuality]] = {}

    for run in runs:
        week = run.timestamp.strftime("%Y-W%W")
        if week not in runs_by_week:
            runs_by_week[week] = []
        runs_by_week[week].append(run)

    for insight in insights:
        week = insight.timestamp.strftime("%Y-W%W")
        if week not in insights_by_week:
            insights_by_week[week] = []
        insights_by_week[week].append(insight)

    if json_output:
        output = {
            "period_days": days,
            "weekly_runs": {
                week: {
                    "count": len(week_runs),
                    "conversions": sum(r.conversions for r in week_runs),
                    "tokens": sum(r.tokens_used for r in week_runs),
                }
                for week, week_runs in runs_by_week.items()
            },
            "weekly_insights": {
                week: {
                    "count": len(week_insights),
                    "avg_quality": (
                        sum(i.quality_score for i in week_insights) / len(week_insights)
                        if week_insights
                        else 0
                    ),
                }
                for week, week_insights in insights_by_week.items()
            },
        }
        click.echo(json.dumps(output, indent=2))
        return

    click.echo(f"\n📈 Trends (last {days} days)")
    click.echo("=" * 60)

    if not runs_by_week and not insights_by_week:
        click.echo("No data available for trend analysis")
        return

    click.echo("\n📅 Weekly Summary:")
    weeks = sorted(set(runs_by_week.keys()) | set(insights_by_week.keys()))

    for week in weeks:
        week_runs = runs_by_week.get(week, [])
        week_insights = insights_by_week.get(week, [])

        run_count = len(week_runs)
        conversions = sum(r.conversions for r in week_runs)
        insight_count = len(week_insights)
        avg_quality = (
            sum(i.quality_score for i in week_insights) / len(week_insights)
            if week_insights
            else 0
        )

        click.echo(
            f"  {week}: runs={run_count}, conversions={conversions}, "
            f"insights={insight_count}, avg_quality={avg_quality:.2f}"
        )


# ============================================================================
# Insights Commands
# ============================================================================


@cli.group()
def insights():
    """View stored insights"""
    pass


@insights.command("list")
@click.option("--limit", "-n", default=20, help="Maximum number to show")
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def insights_list(ctx: click.Context, limit: int, json_output: bool) -> None:
    """List stored insights"""
    storage = InsightStorage()
    stored = storage.list_pending()[:limit]

    if json_output:
        click.echo(json.dumps([s.__dict__ for s in stored], indent=2, default=str))
        return

    if not stored:
        click.echo("No pending insights found")
        return

    click.echo(f"\n💡 Pending Insights ({len(stored)} shown)")
    click.echo("=" * 80)

    for insight in stored:
        click.echo(f"\n📝 {insight.insight_id[:12]}...")
        click.echo(f"   Category: {insight.category}")
        click.echo(f"   Session: {insight.source_session}")
        click.echo(f"   Status: {insight.status}")
        preview = insight.content[:100] if insight.content else ""
        if len(insight.content or "") > 100:
            preview += "..."
        click.echo(f"   Content: {preview}")


# ============================================================================
# Dashboard Command
# ============================================================================


@cli.command()
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON")
@click.pass_context
def dashboard(ctx: click.Context, json_output: bool) -> None:
    """Show overview dashboard of ACE system health"""
    delta_dir = ctx.obj["delta_dir"]
    db = ctx.obj["metrics_db"]

    # Collect data
    deltas = _load_deltas(delta_dir)
    pending_deltas = len([d for d in deltas if d.review_status == "pending"])
    approved_deltas = len([d for d in deltas if d.review_status == "approved"])
    rejected_deltas = len([d for d in deltas if d.review_status == "rejected"])

    runs_7d = []
    insights_7d = []
    if db:
        since = datetime.now() - timedelta(days=7)
        runs_7d = db.get_curation_runs(since=since)
        insights_7d = db.get_insights(since=since)

    if json_output:
        output = {
            "deltas": {
                "total": len(deltas),
                "pending": pending_deltas,
                "approved": approved_deltas,
                "rejected": rejected_deltas,
            },
            "runs_7d": {
                "count": len(runs_7d),
                "conversions": sum(r.conversions for r in runs_7d),
                "tokens": sum(r.tokens_used for r in runs_7d),
            },
            "insights_7d": {
                "count": len(insights_7d),
                "avg_quality": (
                    sum(i.quality_score for i in insights_7d) / len(insights_7d)
                    if insights_7d
                    else 0
                ),
            },
        }
        click.echo(json.dumps(output, indent=2))
        return

    click.echo("\n" + "=" * 60)
    click.echo("   🎯 ACE Dashboard")
    click.echo("=" * 60)

    # Deltas section
    click.echo("\n📋 DELTAS")
    click.echo(f"   Total: {len(deltas)}")
    click.echo(f"   ⏳ Pending:  {pending_deltas}")
    click.echo(f"   ✅ Approved: {approved_deltas}")
    click.echo(f"   ❌ Rejected: {rejected_deltas}")

    # Runs section (7 days)
    click.echo("\n🏃 CURATION RUNS (7 days)")
    if runs_7d:
        total_conversions = sum(r.conversions for r in runs_7d)
        total_insights_processed = sum(r.insights_count for r in runs_7d)
        success_rate = sum(1 for r in runs_7d if r.success) / len(runs_7d) * 100
        click.echo(f"   Runs: {len(runs_7d)}")
        click.echo(f"   Success Rate: {success_rate:.0f}%")
        click.echo(f"   Conversions: {total_conversions}")
        if total_insights_processed > 0:
            conv_rate = total_conversions / total_insights_processed * 100
            click.echo(f"   Conversion Rate: {conv_rate:.1f}%")
    else:
        click.echo("   No runs in last 7 days")

    # Insights section (7 days)
    click.echo("\n💡 INSIGHTS (7 days)")
    if insights_7d:
        avg_quality = sum(i.quality_score for i in insights_7d) / len(insights_7d)
        actionable = sum(1 for i in insights_7d if i.actionable)
        click.echo(f"   Count: {len(insights_7d)}")
        click.echo(f"   Avg Quality: {avg_quality:.2f}")
        click.echo(
            f"   Actionable: {actionable} ({actionable/len(insights_7d)*100:.0f}%)"
        )
    else:
        click.echo("   No insights in last 7 days")

    click.echo("\n" + "=" * 60)


def main():
    """Entry point for CLI"""
    cli()


if __name__ == "__main__":
    main()
