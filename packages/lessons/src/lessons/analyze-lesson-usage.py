#!/usr/bin/env python3
"""
Analyze lesson usage from autonomous run logs.

Tracks which lessons are included over time and generates reports
showing inclusion frequency, patterns, and potential over-inclusion issues.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def parse_log_file(log_path: Path) -> Tuple[Optional[datetime], List[str]]:
    """Parse a single log file and extract included lessons.

    Returns:
        (timestamp, list of lesson titles)
    """
    # Extract timestamp from filename: autonomous-YYYYMMDD-HHMMSS.log
    match = re.search(r"autonomous-(\d{8})-(\d{6})\.log", log_path.name)
    if not match:
        return None, []

    date_str, time_str = match.groups()
    timestamp = datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")

    lessons = []
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

        # Find all "Auto-included X lessons:" sections
        # Pattern: "Auto-included X lessons:" followed by lesson titles
        pattern = r"Auto-included \d+ lessons:\s+((?:.*?\n)+?)(?:Skipped|Assistant:|\[)"

        for match in re.finditer(pattern, content, re.MULTILINE):
            lesson_block = match.group(1)
            # Extract lesson titles (lines with content, removing leading dashes/spaces)
            for line in lesson_block.split("\n"):
                line = line.strip()
                # Skip empty lines, log references, and leading dashes
                if not line or line.startswith("-") or ".py:" in line:
                    continue
                # Clean up the lesson title
                lesson = line.strip("- ").strip()
                # Skip if it's clearly a log reference or artifact
                if lesson and not lesson.endswith(".py:"):
                    lessons.append(lesson)

    return timestamp, lessons


def analyze_logs(logs_dir: Path, days: Optional[int] = None) -> Dict:
    """Analyze all logs in directory.

    Args:
        logs_dir: Path to logs directory
        days: Limit to last N days (None for all)

    Returns:
        Analysis results dictionary
    """
    log_files = sorted(logs_dir.glob("autonomous-*.log"))

    if not log_files:
        return {"error": "No log files found"}

    # Filter by date if requested
    if days:
        cutoff = datetime.now().timestamp() - (days * 86400)
        log_files = [f for f in log_files if f.stat().st_mtime > cutoff]

    # Parse all logs
    all_lessons = []
    lessons_by_date: Dict[str, List[str]] = defaultdict(list)
    sessions_by_date: Dict[str, int] = defaultdict(int)

    for log_file in log_files:
        timestamp, lessons = parse_log_file(log_file)
        if timestamp:
            date_key = timestamp.date().isoformat()
            all_lessons.extend(lessons)
            lessons_by_date[date_key].extend(lessons)
            sessions_by_date[date_key] += 1

    # Calculate statistics
    lesson_counts = Counter(all_lessons)
    total_inclusions = len(all_lessons)
    total_sessions = len(log_files)
    unique_lessons = len(lesson_counts)

    # Top lessons
    top_lessons = lesson_counts.most_common(20)

    # Calculate inclusion rate per session
    inclusion_rates = {
        lesson: count / total_sessions for lesson, count in lesson_counts.items()
    }

    # Identify potential over-inclusion (>50% of sessions)
    over_included = [
        (lesson, count, f"{count / total_sessions * 100:.1f}%")
        for lesson, count in lesson_counts.items()
        if count / total_sessions > 0.5
    ]

    # Timeline data
    timeline = []
    for date in sorted(lessons_by_date.keys()):
        date_lessons = Counter(lessons_by_date[date])
        timeline.append(
            {
                "date": date,
                "sessions": sessions_by_date[date],
                "total_inclusions": len(lessons_by_date[date]),
                "unique_lessons": len(date_lessons),
                "top_3": date_lessons.most_common(3),
            }
        )

    return {
        "summary": {
            "total_sessions": total_sessions,
            "total_inclusions": total_inclusions,
            "unique_lessons": unique_lessons,
            "avg_per_session": total_inclusions / total_sessions
            if total_sessions > 0
            else 0,
        },
        "top_lessons": top_lessons,
        "over_included": over_included,
        "inclusion_rates": inclusion_rates,
        "timeline": timeline,
        "date_range": f"{log_files[0].stem.split('-')[1]} to {log_files[-1].stem.split('-')[1]}"
        if log_files
        else "N/A",
    }


def print_report(results: Dict):
    """Print human-readable analysis report."""

    print("=" * 80)
    print("LESSON USAGE ANALYSIS")
    print("=" * 80)

    # Summary
    summary = results["summary"]
    print("\n📊 Summary")
    print(f"  Date Range: {results['date_range']}")
    print(f"  Total Sessions: {summary['total_sessions']}")
    print(f"  Total Lesson Inclusions: {summary['total_inclusions']}")
    print(f"  Unique Lessons Used: {summary['unique_lessons']}")
    print(f"  Avg Inclusions per Session: {summary['avg_per_session']:.1f}")

    # Top lessons
    print("\n🔝 Top 20 Most Included Lessons")
    print(f"{'Rank':<5} {'Count':<8} {'%':<8} {'Lesson'}")
    print("-" * 80)
    for i, (lesson, count) in enumerate(results["top_lessons"], 1):
        pct = count / summary["total_sessions"] * 100
        print(f"{i:<5} {count:<8} {pct:>6.1f}% {lesson}")

    # Over-included lessons
    if results["over_included"]:
        print("\n⚠️  Potentially Over-Included Lessons (>50% of sessions)")
        print(f"{'Count':<8} {'%':<8} {'Lesson'}")
        print("-" * 80)
        for lesson, count, pct in results["over_included"]:
            print(f"{count:<8} {pct:<8} {lesson}")

    # Timeline
    if results["timeline"]:
        print("\n📈 Timeline (Last 10 Days)")
        print(
            f"{'Date':<12} {'Sessions':<10} {'Inclusions':<12} {'Unique':<8} {'Top Lesson'}"
        )
        print("-" * 80)
        for day in results["timeline"][-10:]:
            top_lesson = day["top_3"][0][0] if day["top_3"] else "N/A"
            # Truncate long lesson titles
            if len(top_lesson) > 40:
                top_lesson = top_lesson[:37] + "..."
            print(
                f"{day['date']:<12} {day['sessions']:<10} {day['total_inclusions']:<12} {day['unique_lessons']:<8} {top_lesson}"
            )


def save_json_report(results: Dict, output_path: Path):
    """Save detailed results as JSON."""
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n💾 Detailed results saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze lesson usage from autonomous run logs"
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=Path("logs"),
        help="Path to logs directory (default: logs/)",
    )
    parser.add_argument("--days", type=int, help="Limit analysis to last N days")
    parser.add_argument("--json", type=Path, help="Save detailed results to JSON file")
    parser.add_argument(
        "--verbose", action="store_true", help="Show detailed lesson inclusion patterns"
    )

    args = parser.parse_args()

    # Analyze logs
    results = analyze_logs(args.logs_dir, args.days)

    if "error" in results:
        print(f"Error: {results['error']}")
        return 1

    # Print report
    print_report(results)

    # Save JSON if requested
    if args.json:
        save_json_report(results, args.json)

    # Verbose mode: show inclusion rate distribution
    if args.verbose:
        print("\n📊 Inclusion Rate Distribution")
        print(f"{'Rate Range':<15} {'Count':<8} {'Lessons'}")
        print("-" * 80)

        rates = results["inclusion_rates"]
        ranges = [
            ("0-10%", 0.0, 0.1),
            ("10-25%", 0.1, 0.25),
            ("25-50%", 0.25, 0.5),
            ("50-75%", 0.5, 0.75),
            ("75-100%", 0.75, 1.0),
        ]

        for label, min_rate, max_rate in ranges:
            lessons_in_range = [
                lesson for lesson, rate in rates.items() if min_rate <= rate < max_rate
            ]
            if lessons_in_range:
                print(
                    f"{label:<15} {len(lessons_in_range):<8} {', '.join(lessons_in_range[:3])}"
                )
                if len(lessons_in_range) > 3:
                    print(f"{'':15} {'':8} ... and {len(lessons_in_range) - 3} more")

    return 0


if __name__ == "__main__":
    exit(main())
