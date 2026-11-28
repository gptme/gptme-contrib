"""Tests for lesson effectiveness tracker."""

import json
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from gptme_lessons_extras.effectiveness_tracker import (
    EffectivenessTracker,
    LessonStats,
    TrackerState,
    main,
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


def _write_log(logs: Path, name: str, lesson: str = "lessons/workflow/a.md") -> None:
    d = logs / name
    d.mkdir(parents=True)
    content = (
        "# Relevant Lessons\n\n## Lesson A\n"
        f"*Path: {lesson}*\n*Matched by: keyword:git*\n"
    )
    (d / "conversation.jsonl").write_text(
        json.dumps({"role": "system", "content": content}) + "\n"
    )


@pytest.mark.parametrize("missing_logs", [False, True], ids=["legacy", "missing-logs"])
def test_cli_reports_rebuild_errors_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_logs: bool
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    _write_log(logs, "2025-10-02-zesty")
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    saved = json.loads(state.read_text())
    del saved["processed_logs"]
    state.write_text(json.dumps(saved))
    original = state.read_bytes()

    args = ["--state-file", str(state)]
    if missing_logs:
        # The default logs path is not validated by Click's --logs-dir option.
        args.append("--full")
        expected = "Cannot rebuild statistics: logs directory unavailable"
    else:
        args.extend(["--logs-dir", str(logs)])
        expected = "Legacy tracker state requires --full"
    result = CliRunner().invoke(main, args)

    assert result.exit_code == 1
    assert f"Error: {expected}" in result.output
    assert isinstance(result.exception, SystemExit)
    assert state.read_bytes() == original


def test_incremental_processes_log_whose_name_sorts_earlier(tmp_path: Path) -> None:
    # #2362 E1: gptme log dirs are "<date>-<random words>", so a log created
    # after the last run can sort before it and was never processed.
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    _write_log(logs, "2025-10-02-zesty-blue-fish")
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()

    _write_log(logs, "2025-10-02-amber-red-owl")
    s = EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    assert s.processed_count == 2
    assert s.lesson_stats["lessons/workflow/a.md"].total_inclusions == 2

    # A third run has nothing new to do
    s = EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    assert s.processed_count == 2


def test_full_run_does_not_double_count(tmp_path: Path) -> None:
    # #2362 E2: --full re-read every log into the loaded totals.
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    _write_log(logs, "2025-10-02-one")
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()

    s = EffectivenessTracker(logs_dir=logs, state_file=state).analyze(incremental=False)
    stats = s.lesson_stats["lessons/workflow/a.md"]
    assert s.total_sessions == 1
    assert s.processed_count == 1
    assert stats.total_inclusions == 1
    assert stats.sessions_included == ["2025-10-02-one"]


def test_legacy_state_preserves_archived_log_statistics(tmp_path: Path) -> None:
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    _write_log(logs, "2025-10-02-amber")
    _write_log(logs, "2025-10-02-zesty")
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    saved = json.loads(state.read_text())
    del saved["processed_logs"]
    state.write_text(json.dumps(saved))
    (logs / "2025-10-02-amber").rename(tmp_path / "archived")
    original = state.read_bytes()

    tracker = EffectivenessTracker(logs_dir=logs, state_file=state)
    with pytest.raises(ValueError, match="Restore archived logs"):
        tracker.analyze()
    assert state.read_bytes() == original
    assert tracker.state.total_sessions == 2
    assert tracker.state.lesson_stats["lessons/workflow/a.md"].total_inclusions == 2


@pytest.mark.parametrize("checkpoint_interval", [1, 100])
@pytest.mark.parametrize("missing", [False, True], ids=["unreadable", "missing"])
def test_full_rebuild_preserves_state_on_unreadable_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_interval: int,
    missing: bool,
) -> None:
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    for name in ["2025-10-02-amber", "2025-10-02-zesty"]:
        _write_log(logs, name)
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    original = state.read_bytes()
    tracker = EffectivenessTracker(logs_dir=logs, state_file=state)
    before = tracker.state.to_dict()
    original_open = open

    def deny_zesty(file, *args, **kwargs):
        if Path(file) == logs / "2025-10-02-zesty" / "conversation.jsonl":
            raise PermissionError("temporarily unreadable")
        return original_open(file, *args, **kwargs)

    conversation = logs / "2025-10-02-zesty" / "conversation.jsonl"
    contents = conversation.read_bytes()
    if missing:
        conversation.unlink()
    with monkeypatch.context() as patch:
        if not missing:
            patch.setattr("builtins.open", deny_zesty)
        with pytest.raises(FileNotFoundError if missing else PermissionError):
            tracker.analyze(incremental=False, checkpoint_interval=checkpoint_interval)
    assert state.read_bytes() == original
    assert tracker.state.to_dict() == before
    if missing:
        conversation.write_bytes(contents)
    rebuilt = tracker.analyze(incremental=False)
    assert rebuilt.total_sessions == rebuilt.processed_count == 2
    assert rebuilt.lesson_stats["lessons/workflow/a.md"].total_inclusions == 2


@pytest.mark.parametrize("legacy", [False, True], ids=["full", "legacy"])
def test_rebuild_preserves_state_when_logs_unavailable(
    tmp_path: Path, legacy: bool
) -> None:
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    _write_log(logs, "2025-10-02-zesty")
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    if legacy:
        saved = json.loads(state.read_text())
        del saved["processed_logs"]
        state.write_text(json.dumps(saved))
    if legacy:
        # Legacy migration requires an explicit full rebuild.
        with pytest.raises(ValueError, match="--full"):
            EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    original = state.read_bytes()
    logs.rename(tmp_path / "offline-logs")

    tracker = EffectivenessTracker(logs_dir=logs, state_file=state)
    before = tracker.state.to_dict()
    with pytest.raises(FileNotFoundError, match="Cannot rebuild"):
        tracker.analyze(incremental=False)
    assert state.read_bytes() == original
    assert tracker.state.to_dict() == before

    # Once logs return, the same tracker can rebuild normally.
    (tmp_path / "offline-logs").rename(logs)
    rebuilt = tracker.analyze(incremental=False)
    assert rebuilt.processed_count == rebuilt.total_sessions == 1
    assert rebuilt.processed_logs == ["2025-10-02-zesty"]


@pytest.mark.parametrize("legacy", [False, True], ids=["full", "legacy"])
@pytest.mark.parametrize("interrupted", [False, True], ids=["limit", "checkpoint"])
def test_rebuild_resumes_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool, interrupted: bool
) -> None:
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    names = ["2025-10-02-amber", "2025-10-02-middle", "2025-10-02-zesty"]
    for name in names:
        _write_log(logs, name)
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    if legacy:
        saved = json.loads(state.read_text())
        del saved["processed_logs"]
        state.write_text(json.dumps(saved))

    tracker = EffectivenessTracker(logs_dir=logs, state_file=state)
    if legacy:
        original = state.read_bytes()
        with pytest.raises(ValueError, match="--full"):
            tracker.analyze(limit=1)
        assert state.read_bytes() == original
    if interrupted:
        save_state = tracker._save_state

        def interrupt_after_checkpoint() -> None:
            save_state()
            raise RuntimeError("interrupted after first checkpoint")

        monkeypatch.setattr(tracker, "_save_state", interrupt_after_checkpoint)
        with pytest.raises(RuntimeError, match="interrupted after first checkpoint"):
            tracker.analyze(incremental=False, checkpoint_interval=1)
    else:
        tracker.analyze(incremental=False, limit=1)

    partial = EffectivenessTracker(logs_dir=logs, state_file=state).state
    assert partial.processed_count == partial.total_sessions == 1
    assert partial.processed_logs == names[:1]
    assert partial.last_processed == names[0]
    assert partial.lesson_stats["lessons/workflow/a.md"].total_inclusions == 1

    # Resume from disk, then repeat: neither remaining logs nor the checkpointed
    # log may be skipped or counted twice.
    for _ in range(2):
        resumed = EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
        assert resumed.processed_count == resumed.total_sessions == len(names)
        assert resumed.processed_logs == names
        assert resumed.last_processed == names[-1]
        stats = resumed.lesson_stats["lessons/workflow/a.md"]
        assert stats.total_inclusions == len(names)
        assert stats.sessions_included == names
        assert stats.keywords_matched == {"git": len(names)}


def test_legacy_state_rebuilds_without_skipping_earlier_log(tmp_path: Path) -> None:
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    old = "2025-10-02-zesty-blue-fish"
    new = "2025-10-02-amber-red-owl"
    _write_log(logs, old)
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    # Simulate the pre-upgrade state, which has counters but no processed set.
    legacy = json.loads(state.read_text())
    del legacy["processed_logs"]
    state.write_text(json.dumps(legacy))
    _write_log(logs, new)

    original = state.read_bytes()
    tracker = EffectivenessTracker(logs_dir=logs, state_file=state)
    with pytest.raises(ValueError, match="--full"):
        tracker.analyze()
    assert state.read_bytes() == original
    s = tracker.analyze(incremental=False)
    assert s.processed_count == 2
    assert s.total_sessions == 2
    assert sorted(s.processed_logs) == [new, old]
    stats = s.lesson_stats["lessons/workflow/a.md"]
    assert stats.total_inclusions == 2
    assert sorted(stats.sessions_included) == [new, old]
    assert stats.keywords_matched == {"git": 2}

    # The rebuilt state persists exact membership, not a guessed watermark.
    s = EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    assert s.processed_count == 2
    assert s.total_sessions == 2
    assert s.lesson_stats["lessons/workflow/a.md"].total_inclusions == 2


def test_pending_log_directory_is_skipped_until_written(tmp_path: Path) -> None:
    # gptme creates the log directory before its first message, and aborted
    # sessions can leave an empty one behind. A stray directory must not abort
    # the run, must not be recorded as processed, and must be picked up once
    # its conversation file appears.
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    _write_log(logs, "2025-10-02-amber")
    pending = logs / "2025-10-02-zesty-pending"
    pending.mkdir(parents=True)

    tracker = EffectivenessTracker(logs_dir=logs, state_file=state)
    s = tracker.analyze()
    assert s.processed_count == 1
    assert s.processed_logs == ["2025-10-02-amber"]

    # A second incremental run still skips it instead of failing.
    s = EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    assert s.processed_count == 1
    assert s.processed_logs == ["2025-10-02-amber"]

    # Once the conversation is written the directory is processed normally.
    (pending / "conversation.jsonl").write_text(
        json.dumps(
            {
                "role": "system",
                "content": (
                    "# Relevant Lessons\n\n## Lesson A\n"
                    "*Path: lessons/workflow/a.md*\n*Matched by: keyword:git*\n"
                ),
            }
        )
        + "\n"
    )
    s = EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    assert s.processed_count == 2
    assert sorted(s.processed_logs) == ["2025-10-02-amber", "2025-10-02-zesty-pending"]
    assert s.lesson_stats["lessons/workflow/a.md"].total_inclusions == 2


def test_full_rebuild_skips_stray_directory(tmp_path: Path) -> None:
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    _write_log(logs, "2025-10-02-amber")
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    (logs / "2025-10-02-empty").mkdir()

    s = EffectivenessTracker(logs_dir=logs, state_file=state).analyze(incremental=False)
    assert s.processed_count == s.total_sessions == 1
    assert s.processed_logs == ["2025-10-02-amber"]


def test_full_rebuild_refuses_missing_counted_log_directory(tmp_path: Path) -> None:
    # Rebuilding without a previously counted directory would silently drop
    # its statistics. Refuse before touching the saved state.
    logs, state = tmp_path / "logs", tmp_path / "state.json"
    _write_log(logs, "2025-10-02-amber")
    _write_log(logs, "2025-10-02-zesty")
    EffectivenessTracker(logs_dir=logs, state_file=state).analyze()
    original = state.read_bytes()
    (logs / "2025-10-02-amber").rename(tmp_path / "archived")

    tracker = EffectivenessTracker(logs_dir=logs, state_file=state)
    before = tracker.state.to_dict()
    with pytest.raises(FileNotFoundError, match="previously counted log"):
        tracker.analyze(incremental=False)
    assert state.read_bytes() == original
    assert tracker.state.to_dict() == before
