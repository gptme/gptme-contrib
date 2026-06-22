"""Tests for gptme_cc_memory.injector — memory injection logic."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any


from gptme_cc_memory.injector import (
    clear_file,
    inject_memories,
    prune_stale_pending_updates,
    read_if_exists,
)

VALID_FEEDBACK_MD = """\
---
name: never-skip-precommit
description: Never bypass pre-commit hooks with --no-verify
metadata:
  type: feedback
---

Never use --no-verify to bypass pre-commit hooks.
"""


class TestReadIfExists:
    def test_reads_file(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        assert read_if_exists(f) == "hello world"

    def test_nonexistent_file(self, tmp_path: Path):
        assert read_if_exists(tmp_path / "nonexistent.txt") == ""

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert read_if_exists(f) == ""


class TestClearFile:
    def test_clears_file(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("some content")
        clear_file(f)
        assert f.read_text() == ""

    def test_nonexistent_file(self, tmp_path: Path):
        clear_file(tmp_path / "nonexistent.txt")  # Should not raise


class TestPruneStalePendingUpdates:
    def test_keeps_recent_date(self):
        today = date(2026, 6, 22)
        content = "## Pending — 2026-06-22 10:00 (session: abc)\nSome update here."
        result = prune_stale_pending_updates(content, today)
        assert "2026-06-22" in result

    def test_prunes_old_date(self):
        today = date(2026, 6, 22)
        content = "## Pending — 2026-06-10 10:00 (session: abc)\nStale update here."
        result = prune_stale_pending_updates(content, today)
        assert result == ""

    def test_mixed_dates(self):
        today = date(2026, 6, 22)
        content = """\
## Pending — 2026-06-10 10:00
Stale update.

## Pending — 2026-06-22 14:00
Fresh update.
"""
        result = prune_stale_pending_updates(content, today)
        assert "Fresh" in result
        assert "Stale" not in result

    def test_keeps_undated_content(self):
        today = date(2026, 6, 22)
        content = "## Some section\nUndated content here."
        result = prune_stale_pending_updates(content, today)
        assert "Undated" in result

    def test_empty_content(self):
        today = date(2026, 6, 22)
        assert prune_stale_pending_updates("", today) == ""


class TestInjectMemories:
    def test_empty_memory_dir(self, tmp_path: Path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        meta_file = tmp_path / "metadata.json"
        result = inject_memories(
            "test prompt",
            memory_dir=mem_dir,
            metadata_file=meta_file,
        )
        assert result is None  # Nothing to inject

    def test_with_guidance(self, tmp_path: Path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        meta_file = tmp_path / "metadata.json"
        guidance = mem_dir / "guidance.md"
        guidance.write_text("Remember to check tests first.")

        result = inject_memories(
            "test prompt",
            memory_dir=mem_dir,
            metadata_file=meta_file,
            guidance_file=guidance,
        )
        assert result is not None
        assert "Remember to check tests first" in result

        # Guidance should be cleared after injection
        assert not guidance.read_text()

    def test_with_pending_items(self, tmp_path: Path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        meta_file = tmp_path / "metadata.json"
        items = mem_dir / "pending-items.md"
        items.write_text("## Pending Items\n- Finish the migration")

        result = inject_memories(
            "test prompt",
            memory_dir=mem_dir,
            metadata_file=meta_file,
            pending_items_file=items,
        )
        assert result is not None
        assert "Finish the migration" in result

    def test_preserves_one_shot_files_when_recording_injections_fails(
        self, tmp_path: Path, monkeypatch: Any
    ):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        meta_file = tmp_path / "metadata.json"
        guidance = mem_dir / "guidance.md"
        pending_context = mem_dir / "pending-session-context.md"
        guidance.write_text("Remember to check tests first.")
        pending_context.write_text("Continue the pre-commit hook review.")
        (mem_dir / "feedback-precommit.md").write_text(VALID_FEEDBACK_MD)

        def raise_recording_error(*args: object, **kwargs: object) -> None:
            raise OSError("metadata state unavailable")

        monkeypatch.setattr(
            "gptme_cc_memory.injector.record_memory_injections",
            raise_recording_error,
        )

        result = inject_memories(
            "Don't bypass pre-commit hooks with --no-verify",
            memory_dir=mem_dir,
            metadata_file=meta_file,
            guidance_file=guidance,
            pending_session_context_file=pending_context,
        )

        assert result is None
        assert guidance.read_text() == "Remember to check tests first."
        assert pending_context.read_text() == "Continue the pre-commit hook review."
