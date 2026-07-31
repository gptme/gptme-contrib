"""Tests for standup_data: journal outcome extraction and --since parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gptme_activity_summary.standup_data import (
    get_standup_context,
    parse_since,
)


class TestParseSince:
    def test_hours(self) -> None:
        now = datetime.now(timezone.utc)
        result = parse_since("24h")
        delta = now - result
        assert 23 * 3600 < delta.total_seconds() < 25 * 3600

    def test_days(self) -> None:
        now = datetime.now(timezone.utc)
        result = parse_since("3d")
        delta = now - result
        assert 2.9 * 86400 < delta.total_seconds() < 3.1 * 86400

    def test_iso_string(self) -> None:
        result = parse_since("2026-07-31T18:00:00Z")
        assert result == datetime(2026, 7, 31, 18, 0, 0, tzinfo=timezone.utc)

    def test_iso_without_z(self) -> None:
        result = parse_since("2026-07-31T18:00:00")
        assert result == datetime(2026, 7, 31, 18, 0, 0, tzinfo=timezone.utc)

    def test_invalid_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            parse_since("not-a-date")


class TestGetStandupContext:
    def _make_journal(self, tmp_path: Path, entries: list[dict]) -> Path:
        """Create a minimal journal directory with fake session files."""
        journal = tmp_path / "journal"
        for entry in entries:
            day_dir = journal / entry["date"]
            day_dir.mkdir(parents=True, exist_ok=True)
            path = day_dir / f"{entry['session']}.md"
            path.write_text(f"**Outcome**: productive — {entry['summary']}\n")
        return journal

    def test_finds_recent_summaries(self, tmp_path: Path) -> None:
        journal = self._make_journal(
            tmp_path,
            [
                {
                    "date": "2026-07-31",
                    "session": "autonomous-session-abcd",
                    "summary": "shipped a cool feature",
                }
            ],
        )
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since)
        assert len(ctx.journal_summaries) == 1
        assert ctx.journal_summaries[0].summary == "shipped a cool feature"
        assert ctx.journal_summaries[0].date == "2026-07-31"

    def test_skips_low_signal(self, tmp_path: Path) -> None:
        journal = self._make_journal(
            tmp_path,
            [
                {"date": "2026-07-31", "session": "s1", "summary": "ran typecheck and lint"},
                {"date": "2026-07-31", "session": "s2", "summary": "shipped real feature"},
            ],
        )
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since)
        summaries = [s.summary for s in ctx.journal_summaries]
        assert "shipped real feature" in summaries
        assert not any("typecheck" in s for s in summaries)

    def test_include_low_signal_as_fallback(self, tmp_path: Path) -> None:
        journal = self._make_journal(
            tmp_path,
            [{"date": "2026-07-31", "session": "s1", "summary": "ran typecheck and lint"}],
        )
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since, include_low_signal=True)
        assert len(ctx.journal_summaries) == 1
        assert ctx.journal_summaries[0].low_signal is True

    def test_skips_self_merges(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal"
        day = journal / "2026-07-31"
        day.mkdir(parents=True)
        (day / "self-merges.md").write_text("**Outcome**: productive — merged stuff\n")
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since)
        assert len(ctx.journal_summaries) == 0

    def test_skips_files_before_since(self, tmp_path: Path) -> None:
        journal = self._make_journal(
            tmp_path,
            [{"date": "2026-07-28", "session": "s1", "summary": "old work"}],
        )
        # Since is after the file was written (we check mtime)
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since)
        assert len(ctx.journal_summaries) == 0

    def test_empty_journal_dir(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal"
        journal.mkdir()
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since)
        assert ctx.journal_summaries == []

    def test_missing_journal_dir(self, tmp_path: Path) -> None:
        ctx = get_standup_context(
            tmp_path / "nonexistent", datetime(2026, 7, 30, tzinfo=timezone.utc)
        )
        assert ctx.journal_summaries == []

    def test_to_dict_schema(self, tmp_path: Path) -> None:
        journal = self._make_journal(
            tmp_path,
            [{"date": "2026-07-31", "session": "s1", "summary": "did work"}],
        )
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since)
        d = ctx.to_dict()
        assert "since" in d
        assert "generated_at" in d
        assert "journal_summaries" in d
        assert isinstance(d["journal_summaries"], list)
        if d["journal_summaries"]:
            s = d["journal_summaries"][0]
            assert "date" in s and "session" in s and "summary" in s and "low_signal" in s

    def test_limit_respected(self, tmp_path: Path) -> None:
        journal = self._make_journal(
            tmp_path,
            [{"date": "2026-07-31", "session": f"s{i}", "summary": f"work {i}"} for i in range(10)],
        )
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since, limit=3)
        assert len(ctx.journal_summaries) <= 3

    def test_ignores_non_date_subdirs(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal"
        journal.mkdir()
        templates = journal / "templates"
        templates.mkdir()
        (templates / "example.md").write_text("**Outcome**: productive — template stuff\n")
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        ctx = get_standup_context(journal, since)
        assert len(ctx.journal_summaries) == 0
