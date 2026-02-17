"""Tests for generate_queue dependency filtering and unblocking power."""

from pathlib import Path

import pytest

from gptodo.generate_queue import QueueGenerator, Task


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with task files for testing."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    return tmp_path


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    """Write a task file with frontmatter."""
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(f"# {name}")
    (tasks_dir / f"{name}.md").write_text("\n".join(lines))


class TestFilterBlockedTasks:
    def test_no_requires(self, workspace: Path) -> None:
        """Tasks without requires should pass through."""
        write_task(workspace / "tasks", "task-a", state="active", priority="high")

        gen = QueueGenerator(workspace)
        tasks = [Task(id="task-a", title="A", priority="high", state="active", source="tasks")]
        result = gen.filter_blocked_tasks(tasks)
        assert len(result) == 1

    def test_resolved_requires(self, workspace: Path) -> None:
        """Tasks whose requires are done should pass through."""
        write_task(workspace / "tasks", "dep-task", state="done", priority="medium")
        write_task(
            workspace / "tasks", "task-a", state="active", priority="high", requires=["dep-task"]
        )

        gen = QueueGenerator(workspace)
        tasks = [
            Task(
                id="task-a",
                title="A",
                priority="high",
                state="active",
                source="tasks",
                requires=["dep-task"],
            )
        ]
        result = gen.filter_blocked_tasks(tasks)
        assert len(result) == 1

    def test_unresolved_requires(self, workspace: Path) -> None:
        """Tasks whose requires are not done should be filtered out."""
        write_task(workspace / "tasks", "dep-task", state="active", priority="medium")
        write_task(
            workspace / "tasks", "task-a", state="active", priority="high", requires=["dep-task"]
        )

        gen = QueueGenerator(workspace)
        tasks = [
            Task(
                id="task-a",
                title="A",
                priority="high",
                state="active",
                source="tasks",
                requires=["dep-task"],
            )
        ]
        result = gen.filter_blocked_tasks(tasks)
        assert len(result) == 0

    def test_cancelled_dep_counts_as_resolved(self, workspace: Path) -> None:
        """Cancelled dependencies should count as resolved."""
        write_task(workspace / "tasks", "dep-task", state="cancelled", priority="medium")

        gen = QueueGenerator(workspace)
        tasks = [
            Task(
                id="task-a",
                title="A",
                priority="high",
                state="active",
                source="tasks",
                requires=["dep-task"],
            )
        ]
        result = gen.filter_blocked_tasks(tasks)
        assert len(result) == 1

    def test_archived_dep_resolved(self, workspace: Path) -> None:
        """Dependencies in archive/ should be checked."""
        archive_dir = workspace / "tasks" / "archive"
        archive_dir.mkdir()
        write_task(archive_dir, "dep-task", state="done", priority="medium")

        gen = QueueGenerator(workspace)
        tasks = [
            Task(
                id="task-a",
                title="A",
                priority="high",
                state="active",
                source="tasks",
                requires=["dep-task"],
            )
        ]
        result = gen.filter_blocked_tasks(tasks)
        assert len(result) == 1

    def test_url_requires_skipped(self, workspace: Path) -> None:
        """URL-based requires should be skipped (no cache)."""
        gen = QueueGenerator(workspace)
        tasks = [
            Task(
                id="task-a",
                title="A",
                priority="high",
                state="active",
                source="tasks",
                requires=["https://github.com/org/repo/issues/1"],
            )
        ]
        result = gen.filter_blocked_tasks(tasks)
        assert len(result) == 1

    def test_mixed_url_and_task_requires(self, workspace: Path) -> None:
        """Tasks with both URL and resolved task-based requires should pass through."""
        write_task(workspace / "tasks", "dep-task", state="done", priority="medium")

        gen = QueueGenerator(workspace)
        tasks = [
            Task(
                id="task-a",
                title="A",
                priority="high",
                state="active",
                source="tasks",
                requires=["dep-task", "https://github.com/org/repo/issues/1"],
            )
        ]
        result = gen.filter_blocked_tasks(tasks)
        assert len(result) == 1

    def test_mixed_url_and_unresolved_task_requires(self, workspace: Path) -> None:
        """Tasks with URL requires but unresolved task requires should be blocked."""
        write_task(workspace / "tasks", "dep-task", state="active", priority="medium")

        gen = QueueGenerator(workspace)
        tasks = [
            Task(
                id="task-a",
                title="A",
                priority="high",
                state="active",
                source="tasks",
                requires=["dep-task", "https://github.com/org/repo/issues/1"],
            )
        ]
        result = gen.filter_blocked_tasks(tasks)
        assert len(result) == 0


class TestComputeUnblockingPower:
    def test_no_dependents(self, workspace: Path) -> None:
        """Tasks that nothing depends on should have 0 unblocking power."""
        write_task(workspace / "tasks", "task-a", state="active", priority="high")

        gen = QueueGenerator(workspace)
        tasks = [Task(id="task-a", title="A", priority="high", state="active", source="tasks")]
        gen.compute_unblocking_power(tasks)
        assert tasks[0].unblocking_power == 0

    def test_direct_dependent(self, workspace: Path) -> None:
        """Tasks with one direct dependent should have unblocking power 1."""
        write_task(workspace / "tasks", "task-a", state="active", priority="high")
        write_task(
            workspace / "tasks", "task-b", state="new", priority="medium", requires=["task-a"]
        )

        gen = QueueGenerator(workspace)
        tasks = [Task(id="task-a", title="A", priority="high", state="active", source="tasks")]
        gen.compute_unblocking_power(tasks)
        assert tasks[0].unblocking_power == 1

    def test_transitive_dependents(self, workspace: Path) -> None:
        """Transitive dependents should be counted."""
        write_task(workspace / "tasks", "task-a", state="active", priority="high")
        write_task(
            workspace / "tasks", "task-b", state="new", priority="medium", requires=["task-a"]
        )
        write_task(
            workspace / "tasks", "task-c", state="new", priority="medium", requires=["task-b"]
        )

        gen = QueueGenerator(workspace)
        tasks = [Task(id="task-a", title="A", priority="high", state="active", source="tasks")]
        gen.compute_unblocking_power(tasks)
        assert tasks[0].unblocking_power == 2

    def test_diamond_dependency_graph(self, workspace: Path) -> None:
        """Diamond graph: A→B→D and A→C→D. D should be counted once, not twice."""
        write_task(workspace / "tasks", "task-a", state="active", priority="high")
        write_task(
            workspace / "tasks", "task-b", state="new", priority="medium", requires=["task-a"]
        )
        write_task(
            workspace / "tasks", "task-c", state="new", priority="medium", requires=["task-a"]
        )
        write_task(
            workspace / "tasks",
            "task-d",
            state="new",
            priority="medium",
            requires=["task-b", "task-c"],
        )

        gen = QueueGenerator(workspace)
        tasks = [Task(id="task-a", title="A", priority="high", state="active", source="tasks")]
        gen.compute_unblocking_power(tasks)
        # B, C, D = 3 unique dependents (D counted once via visited set dedup)
        assert tasks[0].unblocking_power == 3

    def test_done_tasks_not_counted(self, workspace: Path) -> None:
        """Done tasks should not count as dependents."""
        write_task(workspace / "tasks", "task-a", state="active", priority="high")
        write_task(
            workspace / "tasks", "task-b", state="done", priority="medium", requires=["task-a"]
        )

        gen = QueueGenerator(workspace)
        tasks = [Task(id="task-a", title="A", priority="high", state="active", source="tasks")]
        gen.compute_unblocking_power(tasks)
        assert tasks[0].unblocking_power == 0

    def test_github_tasks_skipped(self, workspace: Path) -> None:
        """GitHub-sourced tasks should not have unblocking power computed."""
        gen = QueueGenerator(workspace)
        tasks = [Task(id="issue-1", title="GH", priority="high", state="active", source="github")]
        gen.compute_unblocking_power(tasks)
        assert tasks[0].unblocking_power == 0


class TestPriorityScoreWithUnblocking:
    def test_unblocking_boosts_priority(self) -> None:
        """Tasks with higher unblocking power should score higher."""
        task_a = Task(id="a", title="A", priority="medium", state="active", source="tasks")
        task_b = Task(id="b", title="B", priority="medium", state="active", source="tasks")
        task_a.unblocking_power = 3
        task_b.unblocking_power = 0

        assert task_a.priority_score() > task_b.priority_score()

    def test_unblocking_can_outrank_priority(self) -> None:
        """A medium-priority task unblocking 3 should outrank a high-priority task unblocking 0."""
        task_medium = Task(id="m", title="M", priority="medium", state="active", source="tasks")
        task_high = Task(id="h", title="H", priority="high", state="active", source="tasks")
        task_medium.unblocking_power = 3
        task_high.unblocking_power = 0

        # medium (2) + 3 = 5 > high (3) + 0 = 3
        assert task_medium.priority_score() > task_high.priority_score()
