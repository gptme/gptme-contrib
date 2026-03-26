"""Tests for deptree module: dependency graph, unblocking power, DAG rendering."""

from datetime import datetime
from pathlib import Path

from gptodo.deptree import (
    DependencyNode,
    build_dependency_graph,
    compute_unblocking_power,
    render_full_dag_ascii,
)
from gptodo.utils import SubtaskCount, TaskInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(name: str, state: str = "active", requires: list[str] | None = None) -> TaskInfo:
    """Create a minimal TaskInfo for testing."""
    now = datetime.now()
    return TaskInfo(
        path=Path(f"/tmp/{name}.md"),
        name=name,
        state=state,
        priority=None,
        created=now,
        modified=now,
        requires=requires or [],
        tags=[],
        depends=[],
        related=[],
        parent=None,
        discovered_from=[],
        subtasks=SubtaskCount(0, 0),
        issues=[],
        metadata={},
    )


# ---------------------------------------------------------------------------
# compute_unblocking_power tests
# ---------------------------------------------------------------------------


class TestComputeUnblockingPower:
    def test_no_dependencies(self):
        """Isolated tasks all have power 0."""
        tasks = [make_task("a"), make_task("b"), make_task("c")]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        assert power["a"] == 0
        assert power["b"] == 0
        assert power["c"] == 0

    def test_simple_chain(self):
        """a → b → c: a has power 2, b has power 1, c has power 0."""
        tasks = [
            make_task("a"),
            make_task("b", requires=["a"]),  # b depends on a
            make_task("c", requires=["b"]),  # c depends on b
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        assert power["a"] == 2, "a unblocks b and c transitively"
        assert power["b"] == 1, "b unblocks c"
        assert power["c"] == 0, "c unblocks nothing"

    def test_fan_out(self):
        """a → {b, c, d}: a has power 3."""
        tasks = [
            make_task("a"),
            make_task("b", requires=["a"]),
            make_task("c", requires=["a"]),
            make_task("d", requires=["a"]),
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        assert power["a"] == 3
        assert power["b"] == 0
        assert power["c"] == 0
        assert power["d"] == 0

    def test_fan_in(self):
        """a, b → c: a and b each have power 1, c has power 0."""
        tasks = [
            make_task("a"),
            make_task("b"),
            make_task("c", requires=["a", "b"]),
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        assert power["a"] == 1
        assert power["b"] == 1
        assert power["c"] == 0

    def test_diamond(self):
        """a → {b, c} → d: a has power 3 (b, c, d), b and c each have power 1."""
        tasks = [
            make_task("a"),
            make_task("b", requires=["a"]),
            make_task("c", requires=["a"]),
            make_task("d", requires=["b", "c"]),
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        assert power["a"] == 3, "a unblocks b, c, d"
        assert power["b"] == 1, "b unblocks d"
        assert power["c"] == 1, "c unblocks d"
        assert power["d"] == 0

    def test_excludes_done_by_default(self):
        """Done tasks are excluded from unblocking power count."""
        tasks = [
            make_task("a"),
            make_task("b", state="done", requires=["a"]),
            make_task("c", requires=["a"]),
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        # b is done so excluded; only c counts
        assert power["a"] == 1, "a unblocks c (b is done, excluded)"

    def test_excludes_cancelled_by_default(self):
        """Cancelled tasks are excluded from unblocking power count of their dependencies."""
        tasks = [
            make_task("a"),
            make_task("b", state="cancelled", requires=["a"]),
            make_task("c", requires=["b"]),
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        # b is cancelled → b is NOT counted as a dependent of a → a's power = 0
        assert power["a"] == 0
        # b's required_by includes c (active), so b's unblocking power = 1
        # (b could theoretically unblock c, even though b is cancelled)
        assert power["b"] == 1

    def test_custom_exclude_states(self):
        """Custom exclude_states overrides defaults."""
        tasks = [
            make_task("a"),
            make_task("b", state="waiting", requires=["a"]),
        ]
        nodes = build_dependency_graph(tasks)
        # Default: only done/cancelled excluded → waiting counts
        power_default = compute_unblocking_power(nodes)
        assert power_default["a"] == 1

        # Custom: also exclude waiting
        power_custom = compute_unblocking_power(
            nodes, exclude_states={"done", "cancelled", "waiting"}
        )
        assert power_custom["a"] == 0

    def test_circular_no_infinite_loop(self):
        """Circular deps don't cause infinite recursion."""
        tasks = [
            make_task("a", requires=["b"]),
            make_task("b", requires=["a"]),
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        # Just check it terminates and returns something reasonable
        assert isinstance(power["a"], int)
        assert isinstance(power["b"], int)

    def test_missing_dependency_ignored(self):
        """References to non-existent tasks don't crash."""
        tasks = [
            make_task("a", requires=["nonexistent"]),
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        assert power["a"] == 0


# ---------------------------------------------------------------------------
# render_full_dag_ascii tests
# ---------------------------------------------------------------------------


class TestRenderFullDagAscii:
    def test_empty_graph(self):
        """Empty graph returns empty string."""
        nodes: dict[str, DependencyNode] = {}
        result = render_full_dag_ascii(nodes)
        assert result.strip() == ""

    def test_isolated_tasks_listed(self):
        """Tasks with no deps appear in 'No dependencies' section."""
        tasks = [make_task("a"), make_task("b")]
        nodes = build_dependency_graph(tasks)
        result = render_full_dag_ascii(nodes)
        assert "No dependencies" in result
        assert "a" in result
        assert "b" in result

    def test_chain_rendered(self):
        """Connected tasks appear in the graph."""
        tasks = [
            make_task("base"),
            make_task("child", requires=["base"]),
        ]
        nodes = build_dependency_graph(tasks)
        result = render_full_dag_ascii(nodes)
        assert "base" in result
        assert "child" in result
        # base should appear before child (root first)
        assert result.index("base") < result.index("child")

    def test_power_scores_shown(self):
        """Unblocking power scores appear when provided."""
        tasks = [
            make_task("base"),
            make_task("child", requires=["base"]),
        ]
        nodes = build_dependency_graph(tasks)
        power = compute_unblocking_power(nodes)
        result = render_full_dag_ascii(nodes, unblocking_power=power)
        # base has power 1, should show [1↑]
        assert "1↑" in result

    def test_power_scores_hidden(self):
        """No power scores shown when unblocking_power is None."""
        tasks = [make_task("a"), make_task("b", requires=["a"])]
        nodes = build_dependency_graph(tasks)
        result = render_full_dag_ascii(nodes, unblocking_power=None)
        assert "↑" not in result

    def test_filter_states(self):
        """filter_states hides tasks not in specified states."""
        tasks = [
            make_task("active-task", state="active"),
            make_task("done-task", state="done"),
        ]
        nodes = build_dependency_graph(tasks)
        result = render_full_dag_ascii(nodes, filter_states={"active"})
        assert "active-task" in result
        assert "done-task" not in result

    def test_filter_states_hidden_edges_become_isolated(self):
        """Tasks connected only to filtered-out nodes should render as isolated."""
        tasks = [
            make_task("done-base", state="done"),
            make_task("active-child", state="active", requires=["done-base"]),
        ]
        nodes = build_dependency_graph(tasks)

        result = render_full_dag_ascii(nodes, filter_states={"active"})

        assert result.startswith("── No dependencies ──")
        assert "active-child" in result
        assert "done-base" not in result
