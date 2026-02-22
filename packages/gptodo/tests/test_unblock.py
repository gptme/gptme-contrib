"""Tests for auto-unblocking functionality."""

from datetime import datetime
from pathlib import Path
from typing import List

import frontmatter

from gptodo.unblock import auto_unblock_tasks, find_dependent_tasks
from gptodo.utils import SubtaskCount, TaskInfo


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


class TestFanInCompletion:
    """Tests for fan-in completion aggregation."""

    def test_single_subtask_completes_parent(self, tmp_path):
        """When the only subtask completes, parent should be marked done."""
        from gptodo.unblock import check_fan_in_completion

        tasks_dir = tmp_path

        # Create parent task with one spawned subtask
        create_task_file(
            tasks_dir,
            "parent-task",
            {
                "state": "active",
                "spawned_tasks": ["subtask-1"],
                "coordination_mode": "fan-out-fan-in",
            },
        )

        # Create completed subtask
        create_task_file(
            tasks_dir,
            "subtask-1",
            {
                "state": "done",
                "spawned_from": "parent-task",
            },
        )

        # Create task info objects
        parent = create_task_info(
            "parent-task",
            tasks_dir / "parent-task.md",
            state="active",
            metadata={"spawned_tasks": ["subtask-1"], "coordination_mode": "fan-out-fan-in"},
        )
        parent.spawned_tasks = ["subtask-1"]

        subtask = create_task_info(
            "subtask-1",
            tasks_dir / "subtask-1.md",
            state="done",
            metadata={"spawned_from": "parent-task"},
        )
        subtask.spawned_from = "parent-task"

        # Check fan-in completion
        result = check_fan_in_completion(subtask, [parent, subtask], tasks_dir)

        # Parent should be marked done
        assert result is not None
        assert result[0] == "parent-task"
        assert "fan-in complete" in result[1]

        # Verify parent file was updated
        post = frontmatter.load(tasks_dir / "parent-task.md")
        assert post.metadata["state"] == "done"

    def test_partial_subtasks_do_not_complete_parent(self, tmp_path):
        """When only some subtasks complete, parent should remain active."""
        from gptodo.unblock import check_fan_in_completion

        tasks_dir = tmp_path

        # Create parent task with two spawned subtasks
        create_task_file(
            tasks_dir,
            "parent-task",
            {
                "state": "active",
                "spawned_tasks": ["subtask-1", "subtask-2"],
            },
        )

        # Create one completed subtask
        create_task_file(
            tasks_dir,
            "subtask-1",
            {
                "state": "done",
                "spawned_from": "parent-task",
            },
        )

        # Create one active subtask
        create_task_file(
            tasks_dir,
            "subtask-2",
            {
                "state": "active",
                "spawned_from": "parent-task",
            },
        )

        # Create task info objects
        parent = create_task_info(
            "parent-task",
            tasks_dir / "parent-task.md",
            state="active",
            metadata={"spawned_tasks": ["subtask-1", "subtask-2"]},
        )
        parent.spawned_tasks = ["subtask-1", "subtask-2"]

        subtask1 = create_task_info(
            "subtask-1",
            tasks_dir / "subtask-1.md",
            state="done",
            metadata={"spawned_from": "parent-task"},
        )
        subtask1.spawned_from = "parent-task"

        subtask2 = create_task_info(
            "subtask-2",
            tasks_dir / "subtask-2.md",
            state="active",
            metadata={"spawned_from": "parent-task"},
        )
        subtask2.spawned_from = "parent-task"

        # Check fan-in completion
        result = check_fan_in_completion(subtask1, [parent, subtask1, subtask2], tasks_dir)

        # Parent should NOT be marked done (subtask-2 still active)
        assert result is None

        # Verify parent file was not changed
        post = frontmatter.load(tasks_dir / "parent-task.md")
        assert post.metadata["state"] == "active"

    def test_all_subtasks_complete_marks_parent_done(self, tmp_path):
        """When all subtasks complete, parent should be marked done."""
        from gptodo.unblock import check_fan_in_completion

        tasks_dir = tmp_path

        # Create parent task with two spawned subtasks
        create_task_file(
            tasks_dir,
            "parent-task",
            {
                "state": "active",
                "spawned_tasks": ["subtask-1", "subtask-2"],
            },
        )

        # Create both subtasks as done
        create_task_file(
            tasks_dir,
            "subtask-1",
            {
                "state": "done",
                "spawned_from": "parent-task",
            },
        )
        create_task_file(
            tasks_dir,
            "subtask-2",
            {
                "state": "done",
                "spawned_from": "parent-task",
            },
        )

        # Create task info objects
        parent = create_task_info(
            "parent-task",
            tasks_dir / "parent-task.md",
            state="active",
            metadata={"spawned_tasks": ["subtask-1", "subtask-2"]},
        )
        parent.spawned_tasks = ["subtask-1", "subtask-2"]

        subtask1 = create_task_info(
            "subtask-1",
            tasks_dir / "subtask-1.md",
            state="done",
            metadata={"spawned_from": "parent-task"},
        )
        subtask1.spawned_from = "parent-task"

        subtask2 = create_task_info(
            "subtask-2",
            tasks_dir / "subtask-2.md",
            state="done",
            metadata={"spawned_from": "parent-task"},
        )
        subtask2.spawned_from = "parent-task"

        # Check fan-in completion when the second subtask completes
        result = check_fan_in_completion(subtask2, [parent, subtask1, subtask2], tasks_dir)

        # Parent should be marked done
        assert result is not None
        assert result[0] == "parent-task"
        assert "fan-in complete" in result[1]

        # Verify parent file was updated
        post = frontmatter.load(tasks_dir / "parent-task.md")
        assert post.metadata["state"] == "done"

    def test_task_without_spawned_from_returns_none(self, tmp_path):
        """Tasks without spawned_from should not trigger fan-in."""
        from gptodo.unblock import check_fan_in_completion

        tasks_dir = tmp_path

        # Create a regular task (no spawned_from)
        create_task_file(
            tasks_dir,
            "regular-task",
            {"state": "done"},
        )

        task = create_task_info(
            "regular-task",
            tasks_dir / "regular-task.md",
            state="done",
        )
        # spawned_from is None by default

        result = check_fan_in_completion(task, [task], tasks_dir)

        assert result is None
