"""
gptme Wrapped analytics tools.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from gptme.tools import ToolSpec, ToolUse

logger = logging.getLogger(__name__)


def _get_logs_dir() -> Path:
    """Get the gptme logs directory."""
    try:
        from gptme.dirs import get_logs_dir

        return get_logs_dir()
    except ImportError:
        # Fallback for standalone usage
        from pathlib import Path

        return Path.home() / ".local" / "share" / "gptme" / "logs"


def _analyze_conversation(conv_dir: Path) -> dict[str, Any]:
    """Analyze a single conversation directory."""
    stats: dict[str, Any] = {
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost": 0.0,
        "models": defaultdict(int),
        "first_timestamp": None,
        "last_timestamp": None,
    }

    jsonl_path = conv_dir / "conversation.jsonl"
    if not jsonl_path.exists():
        return stats

    with open(jsonl_path) as f:
        for line in f:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            stats["messages"] += 1

            # Track timestamps
            ts = msg.get("timestamp")
            if ts:
                if stats["first_timestamp"] is None:
                    stats["first_timestamp"] = ts
                stats["last_timestamp"] = ts

            # Extract metadata
            metadata = msg.get("metadata", {})
            if not metadata:
                continue

            stats["input_tokens"] += metadata.get("input_tokens", 0)
            stats["output_tokens"] += metadata.get("output_tokens", 0)
            stats["cache_read_tokens"] += metadata.get("cache_read_tokens", 0)
            stats["cache_creation_tokens"] += metadata.get("cache_creation_tokens", 0)
            stats["cost"] += metadata.get("cost", 0) or 0

            model = metadata.get("model", "unknown")
            if model:
                stats["models"][model] += 1

    return stats


def wrapped_stats(
    year: int | None = None, logs_dir: Path | None = None
) -> dict[str, Any]:
    """
    Get comprehensive statistics for gptme usage.

    Args:
        year: Filter to specific year (default: current year)
        logs_dir: Override logs directory (for testing)

    Returns:
        Dictionary with usage statistics
    """
    if year is None:
        year = datetime.now().year

    if logs_dir is None:
        logs_dir = _get_logs_dir()

    stats: dict[str, Any] = {
        "year": year,
        "conversations": 0,
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost": 0.0,
        "models": defaultdict(lambda: {"count": 0, "tokens": 0, "cost": 0.0}),
        "by_month": defaultdict(lambda: {"conversations": 0, "tokens": 0, "cost": 0.0}),
        "by_day": defaultdict(lambda: {"conversations": 0, "tokens": 0, "cost": 0.0}),
        "hours": defaultdict(int),
        "weekdays": defaultdict(int),
        "conversations_with_metadata": 0,
    }

    year_prefix = str(year)

    for conv_dir in logs_dir.iterdir():
        if not conv_dir.is_dir():
            continue

        # Filter by year (directories are named like 2025-12-25-name or timestamp)
        dir_name = conv_dir.name
        if not dir_name.startswith(year_prefix):
            # Try to parse timestamp directories
            try:
                ts = int(dir_name)
                dt = datetime.fromtimestamp(ts / 1000)
                if dt.year != year:
                    continue
            except (ValueError, OSError):
                continue

        conv_stats = _analyze_conversation(conv_dir)

        if conv_stats["messages"] == 0:
            continue

        stats["conversations"] += 1
        stats["messages"] += conv_stats["messages"]
        stats["input_tokens"] += conv_stats["input_tokens"]
        stats["output_tokens"] += conv_stats["output_tokens"]
        stats["cache_read_tokens"] += conv_stats["cache_read_tokens"]
        stats["cache_creation_tokens"] += conv_stats["cache_creation_tokens"]
        stats["cost"] += conv_stats["cost"]

        # Check if conversation has metadata
        if conv_stats["input_tokens"] > 0 or conv_stats["output_tokens"] > 0:
            stats["conversations_with_metadata"] += 1

        # Aggregate model stats
        for model, count in conv_stats["models"].items():
            stats["models"][model]["count"] += count

        # Track by month/day
        first_ts = conv_stats["first_timestamp"]
        if first_ts:
            try:
                dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                month_key = dt.strftime("%Y-%m")
                day_key = dt.strftime("%Y-%m-%d")

                stats["by_month"][month_key]["conversations"] += 1
                stats["by_month"][month_key]["tokens"] += (
                    conv_stats["input_tokens"] + conv_stats["output_tokens"]
                )
                stats["by_month"][month_key]["cost"] += conv_stats["cost"]

                stats["by_day"][day_key]["conversations"] += 1
                stats["hours"][dt.hour] += 1
                stats["weekdays"][dt.weekday()] += 1
            except (ValueError, AttributeError):
                pass

    # Convert defaultdicts to regular dicts for JSON serialization
    stats["models"] = dict(stats["models"])
    stats["by_month"] = dict(stats["by_month"])
    stats["by_day"] = dict(stats["by_day"])
    stats["hours"] = dict(stats["hours"])
    stats["weekdays"] = dict(stats["weekdays"])

    # Calculate derived metrics
    if stats["input_tokens"] > 0:
        stats["cache_hit_rate"] = stats["cache_read_tokens"] / stats["input_tokens"]
    else:
        stats["cache_hit_rate"] = 0.0

    return stats


def wrapped_report(year: int | None = None, logs_dir: Path | None = None) -> str:
    """
    Generate a formatted "Wrapped" report for gptme usage.

    Args:
        year: Filter to specific year (default: current year)
        logs_dir: Override logs directory (for testing)

    Returns:
        Formatted ASCII report string
    """
    stats = wrapped_stats(year, logs_dir=logs_dir)

    # Format numbers
    def fmt_tokens(n: int) -> str:
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)

    def fmt_cost(c: float) -> str:
        return f"${c:.2f}"

    # Build report
    lines = [
        "",
        f"🎁 gptme Wrapped {stats['year']} 🎁",
        "=" * 40,
        "",
        "📊 Your Year in Numbers:",
        f"  • {stats['conversations']:,} conversations",
        f"  • {stats['messages']:,} messages",
        f"  • {fmt_tokens(stats['input_tokens'])} input tokens",
        f"  • {fmt_tokens(stats['output_tokens'])} output tokens",
        f"  • {fmt_cost(stats['cost'])} total cost",
        "",
    ]

    # Metadata coverage
    if stats["conversations"] > 0:
        coverage = stats["conversations_with_metadata"] / stats["conversations"] * 100
        lines.append(f"📈 Metadata Coverage: {coverage:.0f}%")
        if coverage < 50:
            lines.append("   (Token tracking is recent - more data next year!)")
        lines.append("")

    # Top models
    if stats["models"]:
        lines.append("🤖 Top Models:")
        sorted_models = sorted(
            stats["models"].items(), key=lambda x: x[1]["count"], reverse=True
        )
        total_count = sum(m["count"] for m in stats["models"].values())
        for i, (model, data) in enumerate(sorted_models[:5], 1):
            pct = data["count"] / total_count * 100 if total_count > 0 else 0
            lines.append(f"  {i}. {model} ({pct:.0f}%)")
        lines.append("")

    # Peak usage
    if stats["hours"]:
        peak_hour = max(stats["hours"], key=stats["hours"].get)
        lines.append("⏰ Peak Usage:")
        lines.append(f"  • Most active hour: {peak_hour}:00-{peak_hour+1}:00")

    if stats["weekdays"]:
        days = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        peak_day = max(stats["weekdays"], key=stats["weekdays"].get)
        lines.append(f"  • Most active day: {days[peak_day]}")
        lines.append("")

    # Cache efficiency
    if stats["cache_read_tokens"] > 0:
        lines.append("💾 Cache Efficiency:")
        lines.append(f"  • Cache hit rate: {stats['cache_hit_rate']*100:.0f}%")
        lines.append(f"  • Cached tokens: {fmt_tokens(stats['cache_read_tokens'])}")
        # Rough savings estimate (cached tokens cost ~10% of regular)
        savings = stats["cache_read_tokens"] * 0.9 * 0.000003
        lines.append(f"  • Est. savings: {fmt_cost(savings)}")
        lines.append("")

    # Monthly breakdown
    if stats["by_month"]:
        lines.append("📅 Monthly Breakdown:")
        sorted_months = sorted(stats["by_month"].items())
        max_cost = (
            max(m["cost"] for m in stats["by_month"].values())
            if stats["by_month"]
            else 1
        )
        for month, data in sorted_months:
            bar_len = int(data["cost"] / max_cost * 20) if max_cost > 0 else 0
            bar = "█" * bar_len
            lines.append(f"  {month}: {fmt_cost(data['cost']):>8} {bar}")
        lines.append("")

    lines.append("=" * 40)
    lines.append("Generated by gptme-wrapped plugin")
    lines.append("")

    return "\n".join(lines)


def wrapped_heatmap(year: int | None = None, logs_dir: Path | None = None) -> str:
    """
    Generate a GitHub-style calendar heatmap of gptme activity.

    Args:
        year: Year to display (default: current year)
        logs_dir: Override logs directory (for testing)

    Returns:
        ASCII calendar heatmap
    """
    from datetime import timedelta

    if year is None:
        year = datetime.now().year

    stats = wrapped_stats(year, logs_dir=logs_dir)
    by_day = stats.get("by_day", {})

    # Find max activity for scaling
    max_activity = max((d.get("conversations", 0) for d in by_day.values()), default=1)

    # Intensity characters (empty to full)
    chars = " ░▒▓█"

    def get_char(count: int) -> str:
        if count == 0:
            return chars[0]
        # Scale to 1-4 range
        level = min(4, max(1, int((count / max_activity) * 4)))
        return chars[level]

    # Find first day of year and calculate weeks
    jan1 = datetime(year, 1, 1)
    dec31 = datetime(year, 12, 31)

    # Adjust to start from first Sunday (GitHub style) or Monday
    start_date = jan1 - timedelta(days=jan1.weekday())  # Start from Monday

    # Build week columns
    weeks: list[list[str]] = []
    current_date = start_date

    while current_date <= dec31:
        week = []
        for _ in range(7):  # Mon-Sun
            if current_date.year == year:
                day_key = current_date.strftime("%Y-%m-%d")
                count = by_day.get(day_key, {}).get("conversations", 0)
                week.append(get_char(count))
            else:
                week.append(" ")  # Outside year
            current_date += timedelta(days=1)
        weeks.append(week)

    # Build month labels
    month_labels = "     "
    current_week_start = start_date
    for i, _ in enumerate(weeks):
        week_mid = current_week_start + timedelta(days=3)
        if week_mid.day <= 7 and week_mid.year == year:
            month_labels += week_mid.strftime("%b")[0]
        else:
            month_labels += " "
        current_week_start += timedelta(days=7)

    lines = [
        f"📅 Activity Heatmap {year}",
        "",
        month_labels,
    ]

    # Build rows (7 days)
    day_names = ["Mon", "   ", "Wed", "   ", "Fri", "   ", "Sun"]
    for dow in range(7):
        row = f"{day_names[dow]} "
        for week in weeks:
            row += week[dow]
        lines.append(row)

    # Legend and stats
    lines.extend(
        [
            "",
            f"Legend: {chars[0]}=0  {chars[1]}=low  {chars[2]}=med  {chars[3]}=high  {chars[4]}=max",
            f"Total: {stats['conversations']:,} conversations | Max day: {max_activity}",
        ]
    )

    return "\n".join(lines)


def wrapped_export(
    year: int | None = None, format: str = "json", logs_dir: Path | None = None
) -> str:
    """
    Export wrapped statistics in various formats.

    Args:
        year: Filter to specific year (default: current year)
        format: Output format - "json", "csv", or "html"
        logs_dir: Override logs directory (for testing)

    Returns:
        Exported data as string
    """
    stats = wrapped_stats(year, logs_dir=logs_dir)

    if format == "json":
        return json.dumps(stats, indent=2, default=str)

    elif format == "csv":
        lines = ["metric,value"]
        lines.append(f"year,{stats['year']}")
        lines.append(f"conversations,{stats['conversations']}")
        lines.append(f"messages,{stats['messages']}")
        lines.append(f"input_tokens,{stats['input_tokens']}")
        lines.append(f"output_tokens,{stats['output_tokens']}")
        lines.append(f"cache_read_tokens,{stats['cache_read_tokens']}")
        lines.append(f"total_cost,{stats['cost']:.4f}")
        lines.append(f"cache_hit_rate,{stats['cache_hit_rate']:.4f}")
        return "\n".join(lines)

    elif format == "html":
        report = wrapped_report(year)
        # Simple HTML wrapper
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>gptme Wrapped {stats['year']}</title>
    <style>
        body {{ font-family: monospace; padding: 20px; background: #1a1a2e; color: #eee; }}
        pre {{ white-space: pre-wrap; }}
    </style>
</head>
<body>
<pre>{report}</pre>
</body>
</html>"""
        return html

    else:
        return f"Unknown format: {format}. Use 'json', 'csv', or 'html'."


def examples(tool_format):
    return f"""
### Get your gptme Wrapped report
User: Show me my gptme wrapped for this year
Assistant: I'll generate your gptme Wrapped report.
{ToolUse("ipython", [], "print(wrapped_report())").to_output(tool_format)}

### Get detailed statistics
User: Get my detailed gptme usage stats for 2025
Assistant: I'll fetch the detailed statistics.
{ToolUse("ipython", [], "wrapped_stats(2025)").to_output(tool_format)}

### Export stats to JSON
User: Export my gptme stats as JSON
Assistant: I'll export the statistics.
{ToolUse("ipython", [], "print(wrapped_export(format='json'))").to_output(tool_format)}
"""


tool = ToolSpec(
    name="wrapped",
    desc="Year-end analytics for gptme usage - token counts, costs, model preferences",
    examples=examples,
    functions=[wrapped_stats, wrapped_report, wrapped_heatmap, wrapped_export],
)

__doc__ = tool.get_doc(__doc__)
