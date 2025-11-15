#!/usr/bin/env python3
"""Generate automated review prompts for lesson maintenance.

Analyzes lesson staleness and usage data to generate actionable
review prompts for lesson maintenance.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class LessonInfo:
    """Information about a lesson."""

    path: str
    name: str
    days_stale: int
    usage_refs: Optional[int] = None
    last_used: Optional[str] = None


class ReviewPromptGenerator:
    """Generates review prompts based on lesson data."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self.staleness_report = (
            workspace_root / "knowledge/meta/lesson-staleness-report.md"
        )
        self.analytics_report = (
            workspace_root / "knowledge/meta/lesson-usage-report.json"
        )

    def parse_staleness_data(self) -> List[LessonInfo]:
        """Parse staleness report to extract lesson info."""
        lessons: list[LessonInfo] = []

        if not self.staleness_report.exists():
            print(f"⚠ Staleness report not found: {self.staleness_report}")
            return lessons

        content = self.staleness_report.read_text()

        # Parse lesson entries from markdown
        # Format: ### lessons/category/lesson-name.md
        # Details section: - days_since_modified: N
        pattern = r"### (lessons/[^\n]+\.md)\n.*?days_since_modified: (\d+)"

        for match in re.finditer(pattern, content, re.DOTALL):
            path = match.group(1)
            days = int(match.group(2))
            name = Path(path).stem.replace("-", " ").title()

            lessons.append(LessonInfo(path=path, name=name, days_stale=days))

        return lessons

    def parse_analytics_data(self) -> Dict[str, Dict]:
        """Parse analytics JSON to get usage data."""
        if not self.analytics_report.exists():
            print(f"⚠ Analytics report not found: {self.analytics_report}")
            return {}

        try:
            data = json.loads(self.analytics_report.read_text())
            return {lesson["lesson"]: lesson for lesson in data.get("lessons", [])}
        except Exception as e:
            print(f"⚠ Error parsing analytics: {e}")
            return {}

    def generate_prompts(self) -> List[Dict]:
        """Generate review prompts with priorities."""
        lessons = self.parse_staleness_data()
        analytics = self.parse_analytics_data()

        prompts = []

        for lesson in lessons:
            lesson_key = lesson.path.replace("lessons/", "")
            usage = analytics.get(lesson_key, {})
            refs = usage.get("references", 0)
            last_used = usage.get("last_used")

            lesson.usage_refs = refs if refs > 0 else None
            lesson.last_used = last_used

            prompt = self._generate_prompt_for_lesson(lesson)
            if prompt:
                prompts.append(prompt)

        # Sort by priority
        prompts.sort(key=lambda x: x["priority"], reverse=True)

        return prompts

    def _generate_prompt_for_lesson(self, lesson: LessonInfo) -> Optional[Dict]:
        """Generate review prompt for a single lesson."""
        priority = 0
        reason = []
        action = []

        # High usage + stale = critical review
        if lesson.usage_refs and lesson.usage_refs > 500 and lesson.days_stale > 180:
            priority = 10
            reason.append(f"heavily used ({lesson.usage_refs} refs)")
            reason.append(f"stale ({lesson.days_stale} days)")
            action.append("Review for accuracy and best practices")
            action.append("Update examples if needed")

        # Low/no usage + very stale = archival candidate
        elif (
            not lesson.usage_refs or lesson.usage_refs < 10
        ) and lesson.days_stale > 180:
            priority = 8
            reason.append(f"minimal usage ({lesson.usage_refs or 0} refs)")
            reason.append(f"very stale ({lesson.days_stale} days)")
            action.append("Review for relevance")
            action.append("Consider archiving if outdated")

        # Very stale regardless of usage (catches lessons without analytics)
        elif lesson.days_stale > 240:
            priority = 7
            reason.append(f"extremely stale ({lesson.days_stale} days)")
            action.append("Review for continued relevance")
            action.append("Update or archive if no longer needed")

        # Moderate usage + stale = review
        elif (
            lesson.usage_refs
            and 10 < lesson.usage_refs < 500
            and lesson.days_stale > 120
        ):
            priority = 6
            reason.append(f"moderate usage ({lesson.usage_refs} refs)")
            reason.append(f"stale ({lesson.days_stale} days)")
            action.append("Review and refresh if needed")

        # Moderately stale without analytics data
        elif lesson.days_stale > 150:
            priority = 5
            reason.append(f"stale ({lesson.days_stale} days)")
            reason.append("no usage analytics available")
            action.append("Review and verify still relevant")

        # Return None only if lesson is recent enough
        if not reason:
            return None

        return {
            "priority": priority,
            "lesson": lesson.path,
            "name": lesson.name,
            "reason": ", ".join(reason),
            "action": action,
            "days_stale": lesson.days_stale,
            "usage_refs": lesson.usage_refs or 0,
        }

    def format_report(self, prompts: List[Dict]) -> str:
        """Format prompts as markdown report."""
        report = ["# Lesson Review Prompts\n"]
        report.append(
            f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n"
        )
        report.append(f"**Total prompts**: {len(prompts)}\n")

        if not prompts:
            report.append("\n✅ No lessons requiring immediate review!\n")
            return "\n".join(report)

        # Group by priority
        high = [p for p in prompts if p["priority"] >= 9]
        medium = [p for p in prompts if 6 <= p["priority"] < 9]
        low = [p for p in prompts if p["priority"] < 6]

        if high:
            report.append("\n## 🔴 Critical Priority (Immediate Action)\n")
            for prompt in high:
                report.append(self._format_prompt(prompt))

        if medium:
            report.append("\n## 🟡 Medium Priority (Review Soon)\n")
            for prompt in medium:
                report.append(self._format_prompt(prompt))

        if low:
            report.append("\n## 🟢 Low Priority (Review When Time Permits)\n")
            for prompt in low:
                report.append(self._format_prompt(prompt))

        return "\n".join(report)

    def _format_prompt(self, prompt: Dict) -> str:
        """Format a single prompt."""
        lines = [
            f"\n### {prompt['name']}",
            f"**Path**: `{prompt['lesson']}`",
            f"**Reason**: {prompt['reason']}",
            f"**Staleness**: {prompt['days_stale']} days | **Usage**: {prompt['usage_refs']} refs\n",
            "**Recommended Actions**:",
        ]

        for action in prompt["action"]:
            lines.append(f"- {action}")

        lines.append("")  # Empty line
        return "\n".join(lines)


def main():
    """Main entry point."""
    workspace_root = Path(__file__).parent.parent.parent
    generator = ReviewPromptGenerator(workspace_root)

    print("Generating lesson review prompts...\n")

    prompts = generator.generate_prompts()
    report = generator.format_report(prompts)

    # Save report
    output_path = workspace_root / "knowledge/meta/lesson-review-prompts.md"
    output_path.write_text(report)

    print(f"✅ Generated {len(prompts)} review prompts")
    print(f"📄 Report saved to: {output_path}\n")

    # Show summary
    if prompts:
        high = len([p for p in prompts if p["priority"] >= 9])
        medium = len([p for p in prompts if 6 <= p["priority"] < 9])
        low = len([p for p in prompts if p["priority"] < 6])

        print("Priority breakdown:")
        print(f"  🔴 Critical: {high}")
        print(f"  🟡 Medium: {medium}")
        print(f"  🟢 Low: {low}")


if __name__ == "__main__":
    main()
