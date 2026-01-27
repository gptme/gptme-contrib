"""Tests for auto-unblocking functionality."""

from datetime import datetime
from pathlib import Path
from typing import List

import frontmatter

from gptodo.unblock import find_dependent_tasks, auto_unblock_tasks
from gptodo.utils import TaskInfo, SubtaskCount


def create_task_file(tasks_dir: Path, name: str, metadata: dict, content: str = "") -> Path:
    """Create a task file for testing."""
    task_path = tasks_dir / f"{name}.md"
    post = frontmatter.Post(content=content, **metadata)
    with open(task_path, "w") as f:
        f.write(frontmatter.dumps(post))
    return task_path


def create_task_info(
    name: str,
    path: Path,
    state: str = "active",
    requires: List[str] = None,
    depends: List[str] = None,
    metadata: dict = None,
) -> TaskInfo:
    """Create a TaskInfo object for testing."""
    metadata = metadata or {}
    now = datetime.now()
    return TaskInfo(
        path=path,
        name=name,
        state=state,
        created=now,
        modified=now,
        priority="medium",
        tags=[],
        depends=depends or [],
        requires=requires or [],
        related=[],
        parent=None,
        discovered_from=[],
        subtasks=SubtaskCount(0, 0),
        issues=[],
        metadata=metadata,
    )


class TestFindDependentTasks:
    """Tests for find_dependent_tasks function."""

    def test_finds_task_in_requires(self, tmp_path):
        """Task with requires: [task-a] is found when task-a completes."""
        task_a = create_task_info("task-a", tmp_path / "task-a.md", state="done")
        task_b = create_task_info("task-b", tmp_path / "task-b.md", requires=["task-a"])

        dependent = find_dependent_tasks("task-a", [task_a, task_b])

        assert len(dependent) == 1
        assert dependent[0].name == "task-b"

    def test_finds_task_in_depends(self, tmp_path):
        """Task with depends: [task-a] (deprecated) is found."""
        task_a = create_task_info("task-a", tmp_path / "task-a.md", state="done")
        task_b = create_task_info("task-b", tmp_path / "task-b.md", depends=["task-a"])

        dependent = find_dependent_tasks("task-a", [task_a, task_b])

        assert len(dependent) == 1
        assert dependent[0].name == "task-b"

    def test_finds_task_with_waiting_for(self, tmp_path):
        """Task with waiting_for containing task-a is found."""
        task_a = create_task_info("task-a", tmp_path / "task-a.md", state="done")
        task_b = create_task_info(
            "task-b",
            tmp_path / "task-b.md",
            metadata={"waiting_for": "task-a"},
        )

        dependent = find_dependent_tasks("task-a", [task_a, task_b])

        assert len(dependent) == 1
        assert dependent[0].name == "task-b"

    def test_no_dependent_tasks(self, tmp_path):
        """No dependent tasks when none reference the completed task."""
        task_a = create_task_info("task-a", tmp_path / "task-a.md", state="done")
        task_b = create_task_info("task-b", tmp_path / "task-b.md")

        dependent = find_dependent_tasks("task-a", [task_a, task_b])

        assert len(dependent) == 0


class TestAutoUnblockTasks:
    """Tests for auto_unblock_tasks function."""

    def test_clears_waiting_for(self, tmp_path):
        """waiting_for is cleared when referenced task completes."""
        tasks_dir = tmp_path

        # Create task files
        create_task_file(tasks_dir, "task-a", {"state": "done"})
        task_b_path = create_task_file(
            tasks_dir,
            "task-b",
            {"state": "active", "waiting_for": "task-a", "waiting_since": "2025-01-01"},
        )

        # Create task info objects
        task_a = create_task_info("task-a", tasks_dir / "task-a.md", state="done")
        task_b = create_task_info(
            "task-b",
            task_b_path,
            state="active",
            metadata={"waiting_for": "task-a", "waiting_since": "2025-01-01"},
        )

        # Run auto-unblock
        unblocked = auto_unblock_tasks(["task-a"], [task_a, task_b], tasks_dir)

        # Verify results
        assert len(unblocked) == 1
        assert unblocked[0][0] == "task-b"
        assert "cleared waiting_for" in unblocked[0][1]

        # Verify file was updated
        post = frontmatter.load(task_b_path)
        assert "waiting_for" not in post.metadata
        assert "waiting_since" not in post.metadata

    def test_multiple_completed_tasks(self, tmp_path):
        """Multiple completed tasks unblock their dependents."""
        tasks_dir = tmp_path

        # Create task files
        create_task_file(tasks_dir, "task-a", {"state": "done"})
        create_task_file(tasks_dir, "task-b", {"state": "done"})
        task_c_path = create_task_file(
            tasks_dir,
            "task-c",
            {"state": "active", "waiting_for": "task-a"},
        )
        task_d_path = create_task_file(
            tasks_dir,
            "task-d",
            {"state": "active", "waiting_for": "task-b"},
        )

        # Create task info objects
        tasks = [
            create_task_info("task-a", tasks_dir / "task-a.md", state="done"),
            create_task_info("task-b", tasks_dir / "task-b.md", state="done"),
            create_task_info(
                "task-c",
                task_c_path,
                state="active",
                metadata={"waiting_for": "task-a"},
            ),
            create_task_info(
                "task-d",
                task_d_path,
                state="active",
                metadata={"waiting_for": "task-b"},
            ),
        ]

        # Run auto-unblock for both
        unblocked = auto_unblock_tasks(["task-a", "task-b"], tasks, tasks_dir)

        # Verify both were unblocked
        assert len(unblocked) == 2
        unblocked_names = {u[0] for u in unblocked}
        assert "task-c" in unblocked_names
        assert "task-d" in unblocked_names

    def test_partial_match_waiting_for_not_cleared(self, tmp_path):
        """waiting_for is NOT cleared when it contains more than just the task ID."""
        tasks_dir = tmp_path

        # Create task files - task-b is waiting for "task-a and PR #123"
        create_task_file(tasks_dir, "task-a", {"state": "done"})
        task_b_path = create_task_file(
            tasks_dir,
            "task-b",
            {
                "state": "active",
                "waiting_for": "task-a and PR #123 review",
                "waiting_since": "2025-01-01",
            },
        )

        # Create task info objects
        task_a = create_task_info("task-a", tasks_dir / "task-a.md", state="done")
        task_b = create_task_info(
            "task-b",
            task_b_path,
            state="active",
            metadata={"waiting_for": "task-a and PR #123 review", "waiting_since": "2025-01-01"},
        )

        # Run auto-unblock
        unblocked = auto_unblock_tasks(["task-a"], [task_a, task_b], tasks_dir)

        # Verify results - should note dependency resolved but NOT clear waiting_for
        assert len(unblocked) == 1
        assert unblocked[0][0] == "task-b"
        assert "dependency task-a resolved" in unblocked[0][1]
        assert "still waiting" in unblocked[0][1]

        # Verify file still has waiting_for (not cleared)
        post = frontmatter.load(task_b_path)
        assert "waiting_for" in post.metadata
        assert post.metadata["waiting_for"] == "task-a and PR #123 review"
        assert "waiting_since" in post.metadata

    def test_exact_match_with_whitespace_is_cleared(self, tmp_path):
        """waiting_for with whitespace around task ID is still cleared."""
        tasks_dir = tmp_path

        # Create task files - task-b has whitespace around waiting_for
        create_task_file(tasks_dir, "task-a", {"state": "done"})
        task_b_path = create_task_file(
            tasks_dir,
            "task-b",
            {
                "state": "active",
                "waiting_for": "  task-a  ",  # Extra whitespace
                "waiting_since": "2025-01-01",
            },
        )

        # Create task info objects
        task_a = create_task_info("task-a", tasks_dir / "task-a.md", state="done")
        task_b = create_task_info(
            "task-b",
            task_b_path,
            state="active",
            metadata={"waiting_for": "  task-a  ", "waiting_since": "2025-01-01"},
        )

        # Run auto-unblock
        unblocked = auto_unblock_tasks(["task-a"], [task_a, task_b], tasks_dir)

        # Verify results - should clear since it's an exact match after stripping
        assert len(unblocked) == 1
        assert unblocked[0][0] == "task-b"
        assert "cleared waiting_for" in unblocked[0][1]

        # Verify file was updated
        post = frontmatter.load(task_b_path)
        assert "waiting_for" not in post.metadata
        assert "waiting_since" not in post.metadata
