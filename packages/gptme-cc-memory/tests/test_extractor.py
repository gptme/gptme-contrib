"""Tests for gptme_cc_memory.extractor — heuristic extraction from trajectories."""

from __future__ import annotations

import json
import sys
from pathlib import Path


from gptme_cc_memory.extractor import (
    detect_corrections,
    detect_confirmations,
    detect_guidance_blocks,
    detect_pending_items,
    extract_memories,
    extract_session_context,
    format_pending_items,
    format_pending_updates,
    format_session_context,
)


# Sample trajectory: one correction + confirmation
CORRECTION_TRAJECTORY = [
    {"role": "human", "content": "Don't use --no-verify, fix the pre-commit failure instead"},
    {"role": "assistant", "content": "You're right, I should fix the hook failure."},
    {"role": "human", "content": "Great, that works!"},
]

# Sample trajectory with a pending item
PENDING_TRAJECTORY = [
    {"role": "human", "content": "Let's start the database migration"},
    {"role": "assistant", "content": "I'll begin with the schema changes."},
    {"role": "human", "content": "The next step is to handle the rollback script"},
]

# Sample trajectory with guidance
GUIDANCE_TRAJECTORY = [
    {"role": "human", "content": "Check the code style"},
    {"role": "assistant", "content": "Reminder: always use type hints for function signatures."},
    {"role": "human", "content": "Good."},
]

# Empty trajectory
EMPTY_TRAJECTORY: list[dict] = []


class TestDetectCorrections:
    def test_detects_correction(self):
        result = detect_corrections(CORRECTION_TRAJECTORY)
        assert len(result) >= 1
        assert any("--no-verify" in c.get("context", "") for c in result)

    def test_no_corrections_in_empty(self):
        assert detect_corrections(EMPTY_TRAJECTORY) == []

    def test_no_false_positive_on_positive_messages(self):
        messages = [
            {"role": "human", "content": "This looks great, thanks!"},
            {"role": "human", "content": "Perfect, let's ship it."},
        ]
        result = detect_corrections(messages)
        assert len(result) == 0


class TestDetectConfirmations:
    def test_detects_confirmation(self):
        result = detect_confirmations(CORRECTION_TRAJECTORY)
        assert len(result) >= 1
        assert any("Great" in c.get("full_text", "") for c in result)

    def test_no_confirmations_in_empty(self):
        assert detect_confirmations(EMPTY_TRAJECTORY) == []

    def test_skips_long_messages(self):
        messages = [
            {"role": "human", "content": "Great, that works!" * 50}  # >200 chars
        ]
        result = detect_confirmations(messages)
        assert len(result) == 0


class TestDetectPendingItems:
    def test_detects_pending_items(self):
        result = detect_pending_items(PENDING_TRAJECTORY)
        assert len(result) >= 1

    def test_no_items_in_empty(self):
        assert detect_pending_items(EMPTY_TRAJECTORY) == []


class TestDetectGuidanceBlocks:
    def test_detects_guidance(self):
        result = detect_guidance_blocks(GUIDANCE_TRAJECTORY)
        assert len(result) >= 1
        assert any("type hints" in g for g in result)

    def test_no_guidance_in_empty(self):
        assert detect_guidance_blocks(EMPTY_TRAJECTORY) == []


class TestExtractSessionContext:
    def test_extracts_goal(self):
        result = extract_session_context(CORRECTION_TRAJECTORY)
        assert result["goal"] is not None

    def test_extracts_last_turn(self):
        result = extract_session_context(CORRECTION_TRAJECTORY)
        assert result["last_turn"] is not None

    def test_counts_tool_calls(self):
        messages = [
            {"role": "human", "content": "hello"},
            {"role": "assistant", "content": "Let me check.\nTool Use: cat\nTool Use: grep"},
        ]
        result = extract_session_context(messages)
        assert result["tool_call_count"] == 2

    def test_empty_trajectory(self):
        result = extract_session_context(EMPTY_TRAJECTORY)
        assert result["goal"] is None
        assert result["tool_call_count"] == 0


class TestExtractMemories:
    def test_extract_from_trajectory_file(self, tmp_path: Path):
        traj = tmp_path / "trajectory.jsonl"
        with open(traj, "w") as f:
            for msg in CORRECTION_TRAJECTORY:
                f.write(json.dumps(msg) + "\n")

        result = extract_memories(traj)
        assert len(result.corrections) >= 1
        assert len(result.confirmations) >= 1

    def test_main_writes_pending_files_under_workspace_memory_dir(
        self, tmp_path: Path, monkeypatch
    ):
        from gptme_cc_memory import extractor

        traj = tmp_path / "trajectory.jsonl"
        workspace = tmp_path / "workspace"
        memory_dir = workspace / "memory"

        with open(traj, "w", encoding="utf-8") as f:
            for msg in CORRECTION_TRAJECTORY:
                f.write(json.dumps(msg) + "\n")

        monkeypatch.setenv("GPTME_CC_MEMORY_DIR", str(workspace))
        monkeypatch.setattr(sys, "argv", ["extractor", str(traj)])

        extractor.main()

        assert (memory_dir / "pending-updates.md").exists()
        assert (memory_dir / "pending-session-context.md").exists()


class TestFormatOutput:
    def test_format_pending_updates(self):
        # Test with a mock result
        from gptme_cc_memory.extractor import ExtractionResult

        r = ExtractionResult(
            corrections=[
                {
                    "type": "negative_feedback",
                    "matched": "don't use",
                    "context": "Don't use --no-verify",
                }
            ],
            confirmations=[{"type": "positive_confirmation", "full_text": "Great, that works"}],
        )
        output = format_pending_updates(r)
        assert "don't use" in output
        assert "Great, that works" in output

    def test_format_pending_items(self):
        from gptme_cc_memory.extractor import ExtractionResult

        r = ExtractionResult(pending_items=["Finish the migration", "Write tests"])
        output = format_pending_items(r)
        assert "Finish" in output
        assert "Write tests" in output

    def test_format_session_context(self):
        from gptme_cc_memory.extractor import ExtractionResult

        r = ExtractionResult(
            session_context={
                "goal": "Fix the bug",
                "last_turn": "Done",
                "message_count": 3,
                "tool_call_count": 2,
            }
        )
        output = format_session_context(r)
        assert "Fix the bug" in output
        assert "Messages" in output
