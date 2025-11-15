#!/usr/bin/env python3
"""
Lesson Staleness Checker

Identifies lessons that may need review based on:
- Last modification date (git)
- Last reference date (from analytics)
- Usage frequency

Generates report of stale lessons for manual review.
"""

import sys

import subprocess
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Tuple
import json


def get_lesson_files(lessons_dir: Path) -> List[Path]:
    """Get all lesson markdown files"""
    return sorted(lessons_dir.rglob("*.md"))


def get_git_last_modified(file_path: Path) -> datetime | None:
    """Get last git modification date for file"""
    try:
        cmd = ["git", "log", "-1", "--format=%aI", str(file_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        date_str = result.stdout.strip()
        if date_str:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (subprocess.CalledProcessError, ValueError):
        pass
    return None


def load_lesson_analytics() -> dict[str, Any]:
    """Load lesson usage data from analytics report"""
    analytics_file = Path("knowledge/meta/lesson-usage-report.json")
    if not analytics_file.exists():
        print(
            "Warning: No analytics report found. Run scripts/lesson_analytics.py first."
        )
        return {}

    try:
        with open(analytics_file) as f:
            return json.load(f)  # type: ignore[no-any-return]
    except Exception as e:
        print(f"Error loading analytics: {e}")
        return {}


def check_lesson_staleness(
    file_path: Path,
    analytics: Dict,
    stale_threshold_days: int = 90,
    inactive_threshold_days: int = 60,
) -> Tuple[bool, str, Dict]:
    """
    Check if lesson is stale

    Returns: (is_stale, reason, details)
    """
    details: dict[str, Any] = {}
    reasons = []

    # Get git modification date
    last_modified = get_git_last_modified(file_path)
    if last_modified:
        days_since_modified = (datetime.now(last_modified.tzinfo) - last_modified).days
        details["days_since_modified"] = days_since_modified
        details["last_modified"] = last_modified.date().isoformat()

        if days_since_modified > stale_threshold_days:
            reasons.append(f"Not modified in {days_since_modified} days")

    # Check reference data from analytics
    # Handle both absolute and relative paths
    try:
        rel_path = str(file_path.relative_to(Path.cwd()))
    except ValueError:
        # Already relative or not under cwd
        rel_path = str(file_path)
    lesson_data = analytics.get("by_path", {}).get(rel_path)

    if lesson_data:
        ref_count = lesson_data.get("count", 0)
        last_ref = lesson_data.get("last_referenced")

        details["reference_count"] = ref_count
        details["last_referenced"] = last_ref

        if last_ref:
            try:
                last_ref_date = datetime.fromisoformat(last_ref)
                days_since_ref = (datetime.now() - last_ref_date).days
                details["days_since_referenced"] = days_since_ref

                if days_since_ref > inactive_threshold_days:
                    reasons.append(f"Not referenced in {days_since_ref} days")
            except ValueError:
                pass

        if ref_count == 0:
            reasons.append("Never referenced")
    else:
        reasons.append("No analytics data")

    is_stale = len(reasons) > 0
    reason = "; ".join(reasons) if reasons else "Active"

    return is_stale, reason, details


def generate_report(stale_lessons: List[Tuple[Path, str, Dict]]) -> str:
    """Generate markdown report of stale lessons"""
    report = [
        "# Lesson Staleness Report",
        f"\nGenerated: {datetime.now().isoformat()}",
        f"\nFound {len(stale_lessons)} lessons needing review\n",
        "## Stale Lessons\n",
        "Lessons that haven't been modified or referenced recently:\n",
    ]

    # Sort by staleness (days since modified + days since referenced)
    def staleness_score(item):
        _, _, details = item
        return details.get("days_since_modified", 0) + details.get(
            "days_since_referenced", 0
        )

    sorted_lessons = sorted(stale_lessons, key=staleness_score, reverse=True)

    for file_path, reason, details in sorted_lessons:
        # Handle both absolute and relative paths
        try:
            rel_path = file_path.relative_to(Path.cwd())
        except ValueError:
            rel_path = file_path
        report.append(f"\n### {rel_path}")
        report.append(f"**Reason**: {reason}")
        report.append("\n**Details**:")
        for key, value in details.items():
            report.append(f"- {key}: {value}")

    return "\n".join(report)


def main():
    lessons_dir = Path("lessons")
    if not lessons_dir.exists():
        print("Error: lessons/ directory not found")
        sys.exit(1)

    # Load analytics data
    analytics = load_lesson_analytics()

    # Check all lessons
    lesson_files = get_lesson_files(lessons_dir)
    stale_lessons = []

    print(f"Checking {len(lesson_files)} lessons for staleness...")

    for file_path in lesson_files:
        is_stale, reason, details = check_lesson_staleness(file_path, analytics)
        if is_stale:
            stale_lessons.append((file_path, reason, details))

    # Generate report
    report = generate_report(stale_lessons)

    # Save report
    report_path = Path("knowledge/meta/lesson-staleness-report.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        f.write(report)

    print(f"\n✅ Report saved to: {report_path}")
    print(f"Found {len(stale_lessons)} stale lessons")

    # Print summary
    if stale_lessons:
        print("\nTop 5 stale lessons:")
        for i, (file_path, reason, _) in enumerate(stale_lessons[:5], 1):
            try:
                rel_path = file_path.relative_to(Path.cwd())
            except ValueError:
                rel_path = file_path
            print(f"{i}. {rel_path}")
            print(f"   Reason: {reason}")


if __name__ == "__main__":
    main()
