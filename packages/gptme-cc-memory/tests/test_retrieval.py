"""Tests for gptme_cc_memory.memory_retrieval — scoring and state."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gptme_cc_memory.memory_retrieval import (
    INJECTION_COOLDOWN_MINUTES,
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


class TestArchiveExclusion:
    """MEMORY-archive.md must never surface as a recall result.

    The archive file is cold storage: a concatenation of archived one-liners
    with broad vocabulary that out-scores real entries on nearly every prompt.
    Excluding it at discover time (SPECIAL_MEMORY_FILES) is the fix; these
    tests confirm the exclusion reaches the scoring layer.
    """

    # Large archive body — broad vocabulary would dominate scoring if not excluded
    ARCHIVE_MD = """\
---
name: archived-memories
description: Cold-storage overflow of archived one-liner memory entries
metadata:
  type: feedback
---

Archived: pre-commit hooks bypass --no-verify. Archived: database migration zero downtime.
Archived: Python typing hints function signatures. Archived: git workflow conventional commits.
Archived: pytest testing code coverage. Archived: PostgreSQL migration table schema.
Archived: CI/CD pipeline deployment monitoring. Archived: code review authentication.
"""

    def test_archive_file_not_recalled(self, memory_dir: Path, state_file: Path):
        """MEMORY-archive.md must not appear in recall results even with a highly matching prompt."""
        (memory_dir / "MEMORY-archive.md").write_text(self.ARCHIVE_MD)
        results = select_relevant_memories(
            "Don't use --no-verify to bypass pre-commit hooks. Add Python type hints.",
            memory_dir=memory_dir,
            state_file=state_file,
            limit=5,
        )
        names = [r["name"] for r in results]
        assert "archived-memories" not in names, (
            "MEMORY-archive.md must be excluded from recall but was returned as a result"
        )

    def test_real_memories_still_recalled_with_archive_present(
        self, memory_dir: Path, state_file: Path
    ):
        """Presence of MEMORY-archive.md must not displace real memory entries."""
        (memory_dir / "MEMORY-archive.md").write_text(self.ARCHIVE_MD)
        results = select_relevant_memories(
            "Don't use --no-verify to bypass pre-commit hooks",
            memory_dir=memory_dir,
            state_file=state_file,
            limit=5,
        )
        names = [r["name"] for r in results]
        assert "never-skip-precommit" in names, (
            "Real memories should still be recalled when MEMORY-archive.md is present"
        )


class TestInjectionCooldown:
    PROMPT = "Don't use --no-verify to bypass pre-commit hooks"

    def _select(self, memory_dir: Path, state_file: Path) -> list[str]:
        return [
            r["name"]
            for r in select_relevant_memories(
                self.PROMPT, memory_dir=memory_dir, state_file=state_file, limit=2
            )
        ]

    def _stamp(self, state_file: Path, name: str, minutes_ago: float) -> None:
        stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        state_file.write_text(
            json.dumps({name: {"last_injected": stamp.isoformat(), "injections": 1}})
        )

    def test_first_injection_is_unaffected(self, memory_dir: Path, state_file: Path):
        assert "never-skip-precommit" in self._select(memory_dir, state_file)

    def test_recent_injection_is_suppressed(self, memory_dir: Path, state_file: Path):
        assert "never-skip-precommit" in self._select(memory_dir, state_file)
        self._stamp(state_file, "never-skip-precommit", minutes_ago=1.0)
        assert "never-skip-precommit" not in self._select(memory_dir, state_file)

    def test_cooldown_expiry_re_allows(self, memory_dir: Path, state_file: Path):
        self._stamp(state_file, "never-skip-precommit", INJECTION_COOLDOWN_MINUTES + 5.0)
        assert "never-skip-precommit" in self._select(memory_dir, state_file)


ENTRY_MD_TEMPLATE = """\
---
name: perf-memory-{i:04d}
description: Performance test memory entry number {i:04d} about precommit hooks and typing
metadata:
  type: feedback
---

Entry {i:04d}: Never bypass pre-commit hooks with --no-verify. Always use Python type hints.
"""


class TestSelectRelevantMemoriesPerformance:
    """Regression guard: repeat-decay dict lookup must not blow the <100ms hook budget."""

    N_ENTRIES = 500
    # Budget: 100ms total / 500 candidates = 0.2ms per candidate. Asserting
    # at 1ms/candidate gives 5× headroom for slow CI runners.
    MAX_MS_PER_CANDIDATE = 1.0

    @pytest.fixture
    def large_memory_dir(self, tmp_path: Path) -> Path:
        mem = tmp_path / "large_memory"
        mem.mkdir()
        for i in range(self.N_ENTRIES):
            (mem / f"perf-memory-{i:04d}.md").write_text(ENTRY_MD_TEMPLATE.format(i=i))
        return mem

    def test_select_budget_with_cooldown_state(
        self, large_memory_dir: Path, tmp_path: Path
    ) -> None:
        """select_relevant_memories on 500 files stays under 1ms/candidate with repeat-decay active."""
        state_file = tmp_path / "cc-memory" / "metadata.json"
        state_file.parent.mkdir(parents=True)
        # Mark half the entries as recently injected so the decay branch runs.
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        state: dict = {
            f"perf-memory-{i:04d}": {"last_injected": stamp, "injections": 1}
            for i in range(0, self.N_ENTRIES, 2)
        }
        state_file.write_text(json.dumps(state))

        t0 = time.perf_counter()
        select_relevant_memories(
            "Don't use --no-verify to bypass pre-commit hooks; add Python type hints",
            memory_dir=large_memory_dir,
            state_file=state_file,
            limit=2,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        per_candidate_ms = elapsed_ms / self.N_ENTRIES
        assert per_candidate_ms < self.MAX_MS_PER_CANDIDATE, (
            f"select_relevant_memories too slow: {per_candidate_ms:.3f}ms/candidate "
            f"(total {elapsed_ms:.1f}ms for {self.N_ENTRIES} entries, "
            f"budget {self.MAX_MS_PER_CANDIDATE}ms/candidate)"
        )
