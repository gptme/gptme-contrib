"""Tests for gptme_cc_memory.memory_retrieval — scoring and state."""

from __future__ import annotations

from pathlib import Path

import pytest

from gptme_cc_memory.memory_retrieval import (
    load_memory_state,
    record_memory_injections,
    render_relevant_memory_block,
    save_memory_state,
    select_relevant_memories,
    update_memory_state_from_text,
)

VALID_FEEDBACK_MD = """\
---
name: never-skip-precommit
description: Never bypass pre-commit hooks with --no-verify
metadata:
  type: feedback
---

Never use --no-verify to bypass pre-commit hooks.

**Why:** Prior incident where bypassing hooks caused a broken migration.
**How to apply:** Fix the underlying hook failure; investigate before bypassing.
"""

VALID_PROJECT_MD = """\
---
name: database-migration-sprint
description: Database migration sprint — zero downtime constraint
metadata:
  type: project
---

Current sprint: PostgreSQL migration. Key constraint: zero downtime.
Don't change schema in ways that require table rewrites.
"""

# A memory about Python typing (relevant when user talks about Python)
USER_TYPING_MD = """\
---
name: prefers-python-typing
description: User prefers type hints on all function signatures
metadata:
  type: user
---

Always use Python type hints.

**Why:** The user values type safety and IDE support.
**How to apply:** Add return type annotations to every function definition.
"""


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "feedback-precommit.md").write_text(VALID_FEEDBACK_MD)
    (mem / "project-db-migration.md").write_text(VALID_PROJECT_MD)
    (mem / "user-python-typing.md").write_text(USER_TYPING_MD)
    return mem


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    sf = tmp_path / "cc-memory" / "metadata.json"
    sf.parent.mkdir(parents=True)
    return sf


class TestSelectRelevantMemories:
    def test_relevant_to_precommit(self, memory_dir: Path, state_file: Path):
        results = select_relevant_memories(
            "Don't use --no-verify to bypass pre-commit hooks",
            memory_dir=memory_dir,
            state_file=state_file,
            limit=2,
        )
        assert len(results) >= 1
        assert any(r["name"] == "never-skip-precommit" for r in results)

    def test_relevant_to_database(self, memory_dir: Path, state_file: Path):
        results = select_relevant_memories(
            "Let's work on PostgreSQL migration with zero downtime",
            memory_dir=memory_dir,
            state_file=state_file,
            limit=2,
        )
        assert len(results) >= 1
        assert any(r["name"] == "database-migration-sprint" for r in results)

    def test_relevant_to_typing(self, memory_dir: Path, state_file: Path):
        results = select_relevant_memories(
            "Add type hints to the Python function signatures",
            memory_dir=memory_dir,
            state_file=state_file,
            limit=2,
        )
        assert len(results) >= 1
        assert any("typing" in r["name"] for r in results)

    def test_empty_prompt(self, memory_dir: Path, state_file: Path):
        results = select_relevant_memories(
            "",
            memory_dir=memory_dir,
            state_file=state_file,
        )
        assert results == []

    def test_short_query_no_match(self, memory_dir: Path, state_file: Path):
        results = select_relevant_memories(
            "hello world foo bar",
            memory_dir=memory_dir,
            state_file=state_file,
        )
        assert results == []

    def test_limit_respected(self, memory_dir: Path, state_file: Path):
        results = select_relevant_memories(
            "Don't use --no-verify to bypass pre-commit hooks. "
            "Also add Python type hints. Don't change schema.",
            memory_dir=memory_dir,
            state_file=state_file,
            limit=1,
        )
        assert len(results) == 1


class TestMemoryState:
    def test_load_empty_state(self, state_file: Path):
        assert load_memory_state(state_file) == {}

    def test_save_and_load(self, state_file: Path):
        state = {"memory1": {"confidence": 0.9, "references": 3}}
        save_memory_state(state, state_file)
        assert state_file.exists()
        loaded = load_memory_state(state_file)
        assert loaded["memory1"]["confidence"] == 0.9
        assert loaded["memory1"]["references"] == 3

    def test_record_injection(self, state_file: Path):
        record_memory_injections(["memory-a", "memory-b"], state_file=state_file)
        state = load_memory_state(state_file)
        assert state["memory-a"]["injections"] == 1
        assert state["memory-b"]["injections"] == 1

    def test_injection_increments(self, state_file: Path):
        record_memory_injections(["memory-a"], state_file=state_file)
        record_memory_injections(["memory-a"], state_file=state_file)
        state = load_memory_state(state_file)
        assert state["memory-a"]["injections"] == 2

    def test_corrupted_state_returns_empty(self, state_file: Path):
        state_file.write_text("not valid json")
        assert load_memory_state(state_file) == {}


class TestRenderRelevantMemoryBlock:
    def test_empty_entries(self):
        assert render_relevant_memory_block([]) == ""

    def test_single_entry(self):
        entries = [
            {
                "type": "feedback",
                "name": "never-skip-precommit",
                "confidence": 0.88,
                "recency": 0.95,
                "matched_terms": ["precommit", "hooks"],
                "excerpt": "Never use --no-verify...",
            }
        ]
        result = render_relevant_memory_block(entries)
        assert "<memory_relevant_entries>" in result
        assert "never-skip-precommit" in result
        assert "feedback" in result
        assert "0.88" in result
        assert "0.95" in result
        assert "</memory_relevant_entries>" in result


class TestUpdateMemoryState:
    def test_detect_reference(self, memory_dir: Path, state_file: Path):
        """Text referencing a memory alias should boost its confidence."""
        matched = update_memory_state_from_text(
            "I noticed you used never-skip-precommit — that's a good rule",
            memory_dir=memory_dir,
            state_file=state_file,
        )
        assert "never-skip-precommit" in matched

        state = load_memory_state(state_file)
        entry = state.get("never-skip-precommit", {})
        assert entry.get("references", 0) >= 1

    def test_no_match(self, memory_dir: Path, state_file: Path):
        matched = update_memory_state_from_text(
            "This is totally unrelated content about gardening.",
            memory_dir=memory_dir,
            state_file=state_file,
        )
        assert matched == []
