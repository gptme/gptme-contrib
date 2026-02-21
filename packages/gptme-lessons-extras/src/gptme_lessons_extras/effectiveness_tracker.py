#!/usr/bin/env python3
"""
Lesson Effectiveness Tracker

Post-hoc, resumable analysis of lesson effectiveness across conversation logs.
Tracks which lessons were included and correlates with session outcomes.

Created for: Task implement-post-hoc-lesson-tracking
Origin: Erik's suggestion on gptme-contrib PR #36
"""

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class LessonStats:
    """Statistics for a single lesson's effectiveness."""

    path: str
    name: str
    total_inclusions: int = 0
    sessions_included: list[str] = field(default_factory=list)
    keywords_matched: dict[str, int] = field(default_factory=dict)
    # Outcome correlation (to be enhanced)
    sessions_with_success: int = 0
    sessions_with_failure: int = 0
    # When first/last seen
    first_seen: str | None = None
    last_seen: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LessonStats":
        return cls(**data)


@dataclass
class TrackerState:
    """Persistent state for the effectiveness tracker."""

    version: str = "1.0"
    last_processed: str | None = None  # Last log directory processed
    processed_count: int = 0
    total_sessions: int = 0
    lesson_stats: dict[str, LessonStats] = field(default_factory=dict)
    checkpoint_time: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["lesson_stats"] = {k: v.to_dict() for k, v in self.lesson_stats.items()}
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackerState":
        lesson_stats = {
            k: LessonStats.from_dict(v) for k, v in data.get("lesson_stats", {}).items()
        }
        return cls(
            version=data.get("version", "1.0"),
            last_processed=data.get("last_processed"),
            processed_count=data.get("processed_count", 0),
            total_sessions=data.get("total_sessions", 0),
            lesson_stats=lesson_stats,
            checkpoint_time=data.get("checkpoint_time"),
        )


class EffectivenessTracker:
    """
    Track lesson effectiveness across conversation logs.

    Features:
    - Post-hoc analysis: Can run on existing logs
    - Resumable: Checkpoints progress, can continue where left off
    - Incremental: Only processes new logs since last run
    """

    # Regex patterns for parsing lesson content in logs
    LESSON_HEADER_PATTERN = re.compile(r"^## (.+)$", re.MULTILINE)
    LESSON_PATH_PATTERN = re.compile(r"\*Path: (.+?)\*", re.MULTILINE)
    LESSON_MATCH_PATTERN = re.compile(r"\*Matched by: (.+?)\*", re.MULTILINE)

    def __init__(
        self,
        logs_dir: Path | None = None,
        state_file: Path | None = None,
    ):
        """
        Initialize the tracker.

        Args:
            logs_dir: Directory containing gptme conversation logs
            state_file: Path to persist tracker state (for resumability)
        """
        self.logs_dir = logs_dir or Path.home() / ".local/share/gptme/logs"
        self.state_file = (
            state_file or Path.home() / ".local/share/gptme/lesson_stats.json"
        )
        self.state = self._load_state()

    def _load_state(self) -> TrackerState:
        """Load existing state or create new."""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                return TrackerState.from_dict(data)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Warning: Could not load state file: {e}")
        return TrackerState()

    def _save_state(self) -> None:
        """Save current state to disk."""
        self.state.checkpoint_time = datetime.now().isoformat()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(self.state.to_dict(), f, indent=2)

    def _get_log_dirs(self, since: str | None = None) -> list[Path]:
        """
        Get log directories to process.

        Args:
            since: Only process logs newer than this directory name
        """
        if not self.logs_dir.exists():
            return []

        dirs = sorted(
            [d for d in self.logs_dir.iterdir() if d.is_dir()],
            key=lambda x: x.name,
        )

        if since:
            # Filter to only newer logs
            dirs = [d for d in dirs if d.name > since]

        return dirs

    def _parse_lessons_from_content(
        self, content: str
    ) -> list[dict[str, str | list[str]]]:
        """
        Parse lesson information from a system message content.

        Returns list of dicts with: name, path, keywords
        """
        lessons = []

        # Split by lesson headers (## Lesson Name)
        sections = re.split(r"(?=^## )", content, flags=re.MULTILINE)

        for section in sections:
            if not section.strip():
                continue

            # Get lesson name from header
            header_match = self.LESSON_HEADER_PATTERN.search(section)
            if not header_match:
                continue

            name = header_match.group(1).strip()

            # Get path
            path_match = self.LESSON_PATH_PATTERN.search(section)
            path = path_match.group(1).strip() if path_match else ""

            # Get keywords
            keywords: list[str] = []
            match_match = self.LESSON_MATCH_PATTERN.search(section)
            if match_match:
                keywords_str = match_match.group(1)
                # Parse "keyword:x, keyword:y" format
                keywords = [
                    k.replace("keyword:", "").replace("tool:", "").strip()
                    for k in keywords_str.split(",")
                ]

            if path:  # Only include if we found a path
                lessons.append(
                    {
                        "name": name,
                        "path": path,
                        "keywords": keywords,
                    }
                )

        return lessons

    def _process_log(self, log_dir: Path) -> list[dict[str, str | list[str]]]:
        """
        Process a single conversation log.

        Returns list of lessons found in the conversation.
        """
        conv_file = log_dir / "conversation.jsonl"
        if not conv_file.exists():
            return []

        lessons_found: list[dict[str, str | list[str]]] = []

        try:
            with open(conv_file) as f:
                for line in f:
                    try:
                        msg = json.loads(line)
                        if msg.get("role") != "system":
                            continue

                        content = msg.get("content", "")
                        if "# Relevant Lessons" not in content:
                            continue

                        # Parse lessons from this system message
                        lessons = self._parse_lessons_from_content(content)
                        lessons_found.extend(lessons)

                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            print(f"Warning: Could not read {conv_file}: {e}")

        return lessons_found

    def _update_stats(
        self,
        log_name: str,
        lessons: list[dict[str, str | list[str]]],
    ) -> None:
        """Update lesson statistics with found lessons."""
        seen_in_session: set[str] = set()

        for lesson in lessons:
            path = str(lesson["path"])
            name = str(lesson["name"])
            keywords = lesson.get("keywords", [])

            if path not in self.state.lesson_stats:
                self.state.lesson_stats[path] = LessonStats(path=path, name=name)

            stats = self.state.lesson_stats[path]
            stats.total_inclusions += 1

            # Track keywords
            for kw in keywords:
                if isinstance(kw, str):
                    stats.keywords_matched[kw] = stats.keywords_matched.get(kw, 0) + 1

            # Track session (only once per session per lesson)
            if path not in seen_in_session:
                stats.sessions_included.append(log_name)
                seen_in_session.add(path)

            # Track first/last seen
            if not stats.first_seen or log_name < stats.first_seen:
                stats.first_seen = log_name
            if not stats.last_seen or log_name > stats.last_seen:
                stats.last_seen = log_name

    def analyze(
        self,
        limit: int | None = None,
        incremental: bool = True,
        checkpoint_interval: int = 100,
    ) -> TrackerState:
        """
        Analyze conversation logs for lesson effectiveness.

        Args:
            limit: Maximum number of logs to process
            incremental: Only process new logs since last run
            checkpoint_interval: Save state every N logs

        Returns:
            Updated TrackerState
        """
        since = self.state.last_processed if incremental else None
        log_dirs = self._get_log_dirs(since=since)

        if limit:
            log_dirs = log_dirs[:limit]

        print(f"Processing {len(log_dirs)} log directories...")

        for i, log_dir in enumerate(log_dirs):
            lessons = self._process_log(log_dir)

            if lessons:
                self._update_stats(log_dir.name, lessons)
                self.state.total_sessions += 1

            self.state.processed_count += 1
            self.state.last_processed = log_dir.name

            # Checkpoint periodically
            if (i + 1) % checkpoint_interval == 0:
                print(f"  Checkpoint at {i + 1} logs...")
                self._save_state()

        # Final save
        self._save_state()
        print(
            f"Processed {len(log_dirs)} logs, {self.state.total_sessions} sessions with lessons"
        )

        return self.state

    def report(self, top_n: int = 20) -> str:
        """
        Generate a report of lesson effectiveness.

        Args:
            top_n: Number of top/bottom lessons to include
        """
        lines = [
            "# Lesson Effectiveness Report",
            "",
            f"**Generated**: {datetime.now().isoformat()}",
            f"**Total logs processed**: {self.state.processed_count}",
            f"**Sessions with lessons**: {self.state.total_sessions}",
            f"**Unique lessons tracked**: {len(self.state.lesson_stats)}",
            "",
        ]

        # Sort by inclusion count
        sorted_lessons = sorted(
            self.state.lesson_stats.values(),
            key=lambda x: x.total_inclusions,
            reverse=True,
        )

        # Top lessons by inclusion
        lines.extend(
            [
                f"## Top {top_n} Most Included Lessons",
                "",
                "| Lesson | Inclusions | Sessions | Top Keywords |",
                "|--------|------------|----------|--------------|",
            ]
        )

        for lesson in sorted_lessons[:top_n]:
            top_kw = sorted(
                lesson.keywords_matched.items(), key=lambda x: x[1], reverse=True
            )[:3]
            kw_str = ", ".join(k for k, _ in top_kw)
            sessions = len(lesson.sessions_included)
            lines.append(
                f"| {lesson.name[:40]} | {lesson.total_inclusions} | {sessions} | {kw_str} |"
            )

        # Bottom lessons (least included)
        if len(sorted_lessons) > top_n:
            lines.extend(
                [
                    "",
                    f"## Bottom {top_n} Least Included Lessons",
                    "",
                    "| Lesson | Inclusions | Last Seen |",
                    "|--------|------------|-----------|",
                ]
            )

            for lesson in sorted_lessons[-top_n:]:
                lines.append(
                    f"| {lesson.name[:40]} | {lesson.total_inclusions} | {lesson.last_seen or 'never'} |"
                )

        # Keywords summary
        keyword_totals: dict[str, int] = defaultdict(int)
        for lesson in sorted_lessons:
            for kw, count in lesson.keywords_matched.items():
                keyword_totals[kw] += count

        top_keywords = sorted(keyword_totals.items(), key=lambda x: x[1], reverse=True)[
            :20
        ]

        lines.extend(
            [
                "",
                "## Top Keywords Triggering Lessons",
                "",
                "| Keyword | Total Matches |",
                "|---------|---------------|",
            ]
        )
        for kw, count in top_keywords:
            lines.append(f"| {kw} | {count} |")

        return "\n".join(lines)

    def reset(self) -> None:
        """Reset all tracking state (start fresh)."""
        self.state = TrackerState()
        if self.state_file.exists():
            self.state_file.unlink()


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Track lesson effectiveness across conversation logs"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum logs to process",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Full reprocess (not incremental)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset all tracking state",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate effectiveness report",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Number of top/bottom lessons in report",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=None,
        help="Override logs directory",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Override state file path",
    )

    args = parser.parse_args()

    tracker = EffectivenessTracker(
        logs_dir=args.logs_dir,
        state_file=args.state_file,
    )

    if args.reset:
        tracker.reset()
        print("Tracking state reset.")
        return

    if args.report:
        print(tracker.report(top_n=args.top_n))
        return

    # Run analysis
    tracker.analyze(
        limit=args.limit,
        incremental=not args.full,
    )

    # Print summary report
    print()
    print(tracker.report(top_n=args.top_n))


if __name__ == "__main__":
    main()
