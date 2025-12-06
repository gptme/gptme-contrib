"""Tests for lesson effectiveness tracker."""

import json
import tempfile
from pathlib import Path


from lessons.effectiveness_tracker import (
    EffectivenessTracker,
    LessonStats,
    TrackerState,
)


class TestLessonStats:
    """Tests for LessonStats dataclass."""

    def test_to_dict_roundtrip(self) -> None:
        """Test that to_dict and from_dict are inverses."""
        stats = LessonStats(
            path="/path/to/lesson.md",
            name="Test Lesson",
            total_inclusions=10,
            sessions_included=["session1", "session2"],
            keywords_matched={"keyword1": 5, "keyword2": 3},
            sessions_with_success=7,
            sessions_with_failure=3,
            first_seen="2025-01-01",
            last_seen="2025-01-10",
        )

        result = LessonStats.from_dict(stats.to_dict())

        assert result.path == stats.path
        assert result.name == stats.name
        assert result.total_inclusions == stats.total_inclusions
        assert result.sessions_included == stats.sessions_included
        assert result.keywords_matched == stats.keywords_matched


class TestTrackerState:
    """Tests for TrackerState dataclass."""

    def test_to_dict_roundtrip(self) -> None:
        """Test that to_dict and from_dict are inverses."""
        state = TrackerState(
            version="1.0",
            last_processed="log-123",
            processed_count=100,
            total_sessions=50,
            lesson_stats={
                "/path/lesson.md": LessonStats(
                    path="/path/lesson.md",
                    name="Test",
                    total_inclusions=5,
                )
            },
        )

        result = TrackerState.from_dict(state.to_dict())

        assert result.version == state.version
        assert result.last_processed == state.last_processed
        assert result.processed_count == state.processed_count
        assert len(result.lesson_stats) == 1
        assert "/path/lesson.md" in result.lesson_stats


class TestEffectivenessTracker:
    """Tests for EffectivenessTracker."""

    def test_parse_lessons_from_content(self) -> None:
        """Test lesson parsing from system message content."""
        tracker = EffectivenessTracker(
            logs_dir=Path("/nonexistent"),
            state_file=Path("/tmp/test_state.json"),
        )

        content = """# Relevant Lessons

## Test Lesson One

*Path: /workspace/lessons/workflow/test-one.md*

*Category: workflow*

*Matched by: keyword:test, keyword:example*

# Test Lesson One

## Rule
Test rule here.

## Another Lesson

*Path: /workspace/lessons/tools/test-two.md*

*Matched by: tool:shell*

# Another Lesson

## Rule
Another rule.
"""

        lessons = tracker._parse_lessons_from_content(content)

        assert len(lessons) >= 2
        paths = [lesson["path"] for lesson in lessons]
        assert "/workspace/lessons/workflow/test-one.md" in paths
        assert "/workspace/lessons/tools/test-two.md" in paths

    def test_state_persistence(self) -> None:
        """Test that state is saved and loaded correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()

            # Create tracker and modify state
            tracker = EffectivenessTracker(
                logs_dir=logs_dir,
                state_file=state_file,
            )
            tracker.state.processed_count = 42
            tracker.state.lesson_stats["/test.md"] = LessonStats(
                path="/test.md", name="Test", total_inclusions=5
            )
            tracker._save_state()

            # Create new tracker and verify state loaded
            tracker2 = EffectivenessTracker(
                logs_dir=logs_dir,
                state_file=state_file,
            )

            assert tracker2.state.processed_count == 42
            assert "/test.md" in tracker2.state.lesson_stats
            assert tracker2.state.lesson_stats["/test.md"].total_inclusions == 5

    def test_reset(self) -> None:
        """Test that reset clears state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()

            # Create and populate state
            tracker = EffectivenessTracker(
                logs_dir=logs_dir,
                state_file=state_file,
            )
            tracker.state.processed_count = 100
            tracker._save_state()

            # Reset
            tracker.reset()

            assert tracker.state.processed_count == 0
            assert not state_file.exists()

    def test_report_generation(self) -> None:
        """Test that report generates without errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = EffectivenessTracker(
                logs_dir=Path(tmpdir),
                state_file=Path(tmpdir) / "state.json",
            )

            # Add some test data
            tracker.state.lesson_stats["/test.md"] = LessonStats(
                path="/test.md",
                name="Test Lesson",
                total_inclusions=10,
                sessions_included=["s1", "s2"],
                keywords_matched={"test": 5, "example": 3},
            )

            report = tracker.report(top_n=5)

            assert "# Lesson Effectiveness Report" in report
            assert "Test Lesson" in report
            assert "test" in report  # keyword

    def test_process_log_with_lessons(self) -> None:
        """Test processing a log file with lesson content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            log_dir = logs_dir / "2025-01-01-test-log"
            log_dir.mkdir()

            # Create conversation.jsonl with lesson content
            conv_file = log_dir / "conversation.jsonl"
            messages = [
                {"role": "system", "content": "System prompt"},
                {
                    "role": "system",
                    "content": """# Relevant Lessons

## Test Lesson

*Path: /lessons/test.md*

*Matched by: keyword:test*

# Test Lesson

## Rule
Test rule.
""",
                },
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ]

            with open(conv_file, "w") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

            tracker = EffectivenessTracker(
                logs_dir=logs_dir,
                state_file=Path(tmpdir) / "state.json",
            )

            lessons = tracker._process_log(log_dir)

            assert len(lessons) >= 1
            assert any(lesson["path"] == "/lessons/test.md" for lesson in lessons)
