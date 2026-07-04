"""Tests for --pool / --exclude-pool filtering across gptodo selection subcommands."""

import json
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import load_tasks, task_matches_pool_filter, task_pool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    """Write a minimal task file with YAML frontmatter."""
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", f"# {name}"])
    (tasks_dir / f"{name}.md").write_text("\n".join(lines))


def cli_runner_separate_stderr() -> CliRunner:
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


# ---------------------------------------------------------------------------
# task_pool() helper — unit tests covering all three frontier signals
# ---------------------------------------------------------------------------


class TestTaskPool:
    def test_general_task_returns_general(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(tasks_dir, "do-something", state="backlog", created="2026-01-01T00:00:00")
        task = load_tasks(tasks_dir)[0]
        assert task_pool(task) == "general"

    def test_pool_frontier_field_returns_frontier(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(
            tasks_dir, "my-task", state="backlog", created="2026-01-01T00:00:00", pool="frontier"
        )
        task = load_tasks(tasks_dir)[0]
        assert task_pool(task) == "frontier"

    def test_pool_general_field_returns_general(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(
            tasks_dir, "my-task", state="backlog", created="2026-01-01T00:00:00", pool="general"
        )
        task = load_tasks(tasks_dir)[0]
        assert task_pool(task) == "general"

    def test_frontier_id_prefix_returns_frontier(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(tasks_dir, "frontier-big-design", state="backlog", created="2026-01-01T00:00:00")
        task = load_tasks(tasks_dir)[0]
        assert task_pool(task) == "frontier"

    def test_explicit_pool_general_overrides_frontier_prefix(self, tmp_path: Path) -> None:
        """Explicit pool:general frontmatter wins over implicit frontier- id prefix."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(
            tasks_dir,
            "frontier-override-test",
            state="backlog",
            created="2026-01-01T00:00:00",
            pool="general",
        )
        task = load_tasks(tasks_dir)[0]
        assert task_pool(task) == "general"

    def test_explicit_pool_general_overrides_frontier_tag(self, tmp_path: Path) -> None:
        """Explicit pool:general frontmatter wins over implicit frontier tag."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(
            tasks_dir,
            "some-task",
            state="backlog",
            created="2026-01-01T00:00:00",
            pool="general",
            tags=["frontier", "feature"],
        )
        task = load_tasks(tasks_dir)[0]
        assert task_pool(task) == "general"

    def test_frontier_tag_returns_frontier(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(
            tasks_dir,
            "some-task",
            state="backlog",
            created="2026-01-01T00:00:00",
            tags=["feature", "frontier"],
        )
        task = load_tasks(tasks_dir)[0]
        assert task_pool(task) == "frontier"

    def test_frontier_routing_tag_does_not_trigger(self, tmp_path: Path) -> None:
        """frontier-routing tag must NOT be treated as pool=frontier (regression guard)."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(
            tasks_dir,
            "gptodo-next-pool-filter",
            state="active",
            created="2026-07-04T00:00:00",
            tags=["gptodo", "tooling", "frontier-routing", "routing", "cli"],
        )
        task = load_tasks(tasks_dir)[0]
        assert task_pool(task) == "general"


# ---------------------------------------------------------------------------
# task_matches_pool_filter() — unit tests
# ---------------------------------------------------------------------------


class TestTaskMatchesPoolFilter:
    def _make_task(self, tmp_path: Path, name: str, **meta: object):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir(exist_ok=True)
        write_task(tasks_dir, name, state="backlog", created="2026-01-01T00:00:00", **meta)
        tasks = {t.name: t for t in load_tasks(tasks_dir)}
        return tasks[name]

    def test_no_filter_passes_all(self, tmp_path: Path) -> None:
        t = self._make_task(tmp_path, "anything")
        assert task_matches_pool_filter(t) is True

    def test_pool_frontier_passes_frontier_task(self, tmp_path: Path) -> None:
        t = self._make_task(tmp_path, "frontier-x", pool="frontier")
        assert task_matches_pool_filter(t, pool="frontier") is True

    def test_pool_frontier_blocks_general_task(self, tmp_path: Path) -> None:
        t = self._make_task(tmp_path, "ordinary-task")
        assert task_matches_pool_filter(t, pool="frontier") is False

    def test_pool_general_blocks_frontier_task(self, tmp_path: Path) -> None:
        t = self._make_task(tmp_path, "frontier-x", pool="frontier")
        assert task_matches_pool_filter(t, pool="general") is False

    def test_exclude_pool_frontier_blocks_frontier_task(self, tmp_path: Path) -> None:
        t = self._make_task(tmp_path, "frontier-x", pool="frontier")
        assert task_matches_pool_filter(t, exclude_pool="frontier") is False

    def test_exclude_pool_frontier_passes_general_task(self, tmp_path: Path) -> None:
        t = self._make_task(tmp_path, "ordinary-task")
        assert task_matches_pool_filter(t, exclude_pool="frontier") is True


# ---------------------------------------------------------------------------
# CLI integration — gptodo ready --pool / --exclude-pool
# ---------------------------------------------------------------------------


class TestReadyPoolFilter:
    def setup_tasks(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(tasks_dir, "general-work", state="backlog", created="2026-01-01T00:00:00")
        write_task(
            tasks_dir,
            "frontier-design",
            state="backlog",
            created="2026-01-01T00:00:00",
            pool="frontier",
        )
        write_task(
            tasks_dir,
            "tag-frontier-task",
            state="backlog",
            created="2026-01-01T00:00:00",
            tags=["frontier"],
        )

    def test_pool_frontier_returns_only_frontier_tasks(self, tmp_path: Path, monkeypatch) -> None:
        self.setup_tasks(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["ready", "--json", "--pool", "frontier"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        ids = [t["id"] for t in data["ready_tasks"]]
        assert "general-work" not in ids
        assert "frontier-design" in ids
        assert "tag-frontier-task" in ids

    def test_exclude_pool_frontier_returns_only_general_tasks(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self.setup_tasks(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["ready", "--json", "--exclude-pool", "frontier"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        ids = [t["id"] for t in data["ready_tasks"]]
        assert "general-work" in ids
        assert "frontier-design" not in ids
        assert "tag-frontier-task" not in ids

    def test_no_pool_filter_returns_all(self, tmp_path: Path, monkeypatch) -> None:
        self.setup_tasks(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["ready", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        ids = [t["id"] for t in data["ready_tasks"]]
        assert "general-work" in ids
        assert "frontier-design" in ids


# ---------------------------------------------------------------------------
# CLI integration — gptodo next --pool / --exclude-pool
# ---------------------------------------------------------------------------


class TestNextPoolFilter:
    def setup_tasks(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(tasks_dir, "general-work", state="backlog", created="2026-01-01T00:00:00")
        write_task(
            tasks_dir,
            "frontier-task",
            state="backlog",
            created="2026-01-01T00:00:00",
            pool="frontier",
        )

    def test_next_pool_frontier_returns_frontier_task(self, tmp_path: Path, monkeypatch) -> None:
        self.setup_tasks(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["next", "--json", "--pool", "frontier"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["next_task"] is not None
        assert data["next_task"]["id"] == "frontier-task"

    def test_next_exclude_pool_frontier_returns_general(self, tmp_path: Path, monkeypatch) -> None:
        self.setup_tasks(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["next", "--json", "--exclude-pool", "frontier"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["next_task"] is not None
        assert data["next_task"]["id"] == "general-work"

    def test_next_pool_frontier_returns_none_when_no_frontier(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(tasks_dir, "general-only", state="backlog", created="2026-01-01T00:00:00")
        monkeypatch.chdir(tmp_path)
        runner = cli_runner_separate_stderr()
        result = runner.invoke(cli, ["next", "--json", "--pool", "frontier"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["next_task"] is None
        assert "No new or active tasks found" in result.stderr


# ---------------------------------------------------------------------------
# task_to_dict pool field — verify it appears in JSON output
# ---------------------------------------------------------------------------


class TestTaskToDictPool:
    def test_pool_field_in_status_json(self, tmp_path: Path, monkeypatch) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(
            tasks_dir,
            "frontier-task",
            state="backlog",
            created="2026-01-01T00:00:00",
            pool="frontier",
        )
        write_task(tasks_dir, "general-task", state="backlog", created="2026-01-01T00:00:00")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        tasks_by_id = {t["id"]: t for t in data["tasks"]}
        assert tasks_by_id["frontier-task"]["pool"] == "frontier"
        assert tasks_by_id["general-task"]["pool"] == "general"

    def test_status_pool_filter_requires_json(self, tmp_path: Path, monkeypatch) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(tasks_dir, "frontier-task", state="backlog", created="2026-01-01T00:00:00")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--pool", "frontier"])
        assert result.exit_code == 2
        assert "--pool/--exclude-pool are only supported with --json" in result.stderr

    def test_status_pool_filter_recomputes_summary(self, tmp_path: Path, monkeypatch) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        write_task(
            tasks_dir,
            "frontier-active",
            state="active",
            created="2026-01-01T00:00:00",
            pool="frontier",
        )
        write_task(tasks_dir, "frontier-broken", state="backlog", pool="frontier")
        write_task(tasks_dir, "general-active", state="active", created="2026-01-01T00:00:00")

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--json", "--pool", "frontier"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert {t["id"] for t in data["tasks"]} == {"frontier-active", "frontier-broken"}
        assert data["summary"] == {
            "total": 2,
            "by_state": {"active": 1, "backlog": 1},
            "issues": 1,
            "untracked": 0,
        }


# ---------------------------------------------------------------------------
# Regression: frontier-routing tag must NOT be treated as pool=frontier
# ---------------------------------------------------------------------------


def test_frontier_routing_tag_is_general_pool(tmp_path: Path, monkeypatch) -> None:
    """frontier-routing tag must not trigger the frontier pool claim gate.

    This is a regression guard for the 2026-07-04 incident where the
    gptodo-next-pool-filter task itself was incorrectly denied a claim
    because its 'frontier' tag was a renamed 'frontier-routing' tag.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "gptodo-next-pool-filter",
        state="active",
        created="2026-07-04T00:00:00",
        tags=["gptodo", "tooling", "frontier-routing", "routing", "cli"],
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["ready", "--json", "--pool", "general", "--state", "active"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    ids = [t["id"] for t in data["ready_tasks"]]
    assert "gptodo-next-pool-filter" in ids
