#!/usr/bin/env python3
"""Dependency tree visualization for tasks.

Provides ASCII and Mermaid visualizations of task dependencies,
showing what each task requires and what depends on it.
Also provides unblocking power computation for priority scoring.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .utils import TaskInfo, load_tasks


@dataclass
class DependencyNode:
    """Node in the dependency tree."""

    name: str
    state: str
    is_external: bool = False
    requires: list["DependencyNode"] = field(default_factory=list)
    required_by: list["DependencyNode"] = field(default_factory=list)
    depth: int = 0
    is_circular: bool = False


def build_dependency_graph(
    tasks: list[TaskInfo],
) -> dict[str, DependencyNode]:
    """Build a full dependency graph from task list.

    Args:
        tasks: List of all tasks

    Returns:
        Dictionary mapping task names to DependencyNode objects
    """
    # Create nodes
    nodes: dict[str, DependencyNode] = {}
    for task in tasks:
        nodes[task.name] = DependencyNode(
            name=task.name,
            state=task.state or "unknown",
        )

    # Build relationships
    for task in tasks:
        node = nodes[task.name]

        # Process requires (what this task depends on)
        for req in task.requires:
            if req.startswith(("http://", "https://")):
                # URL dependency - create a placeholder node
                short_name = req[:50] + "..." if len(req) > 50 else req
                if req not in nodes:
                    nodes[req] = DependencyNode(
                        name=short_name,
                        state="external",
                        is_external=True,
                    )
                nodes[req].required_by.append(node)
                node.requires.append(nodes[req])
            elif req in nodes:
                nodes[req].required_by.append(node)
                node.requires.append(nodes[req])

    return nodes


def detect_circular_dependencies(
    nodes: dict[str, DependencyNode],
) -> list[list[str]]:
    """Detect circular dependencies in the graph.

    Returns:
        List of cycles found (each cycle is a list of task names)
    """
    cycles: list[list[str]] = []
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def dfs(name: str, path: list[str]) -> None:
        if name in rec_stack:
            # Found cycle
            cycle_start = path.index(name)
            cycle = path[cycle_start:] + [name]
            cycles.append(cycle)
            if name in nodes:
                nodes[name].is_circular = True
            return

        if name in visited:
            return

        visited.add(name)
        rec_stack.add(name)
        path.append(name)

        node = nodes.get(name)
        if node:
            for req_node in node.requires:
                dfs(req_node.name, path.copy())

        rec_stack.remove(name)

    for name in nodes:
        dfs(name, [])

    return cycles


def compute_unblocking_power(
    nodes: dict[str, DependencyNode],
    exclude_states: set[str] | None = None,
) -> dict[str, int]:
    """Compute how many tasks each task transitively unblocks.

    A task's unblocking power = number of non-terminal tasks that transitively
    depend on it (i.e., tasks that can't start until this one is done).
    Higher scores mean completing this task would unlock more downstream work.

    Args:
        nodes: Dependency graph from build_dependency_graph
        exclude_states: Task states to exclude from count
                        (default: {"done", "cancelled"} — terminal states)

    Returns:
        Dict mapping task name to unblocking power score (0 = unblocks nothing)
    """
    if exclude_states is None:
        exclude_states = {"done", "cancelled"}

    def _dependents(name: str, seen: set[str]) -> set[str]:
        """Collect unique transitive dependents (deduplicates diamond patterns)."""
        result: set[str] = set()
        node = nodes.get(name)
        if not node:
            return result
        for dep in node.required_by:
            if dep.name in seen:
                continue
            if dep.state not in exclude_states:
                result.add(dep.name)
                seen.add(dep.name)
                result.update(_dependents(dep.name, seen))
        return result

    return {name: len(_dependents(name, set())) for name in nodes}


def render_full_dag_ascii(
    nodes: dict[str, DependencyNode],
    unblocking_power: dict[str, int] | None = None,
    filter_states: set[str] | None = None,
) -> str:
    """Render the full workspace dependency graph as ASCII.

    Shows all tasks with dependencies, grouped by whether they have any edges.
    Tasks with no dependencies or dependents are listed compactly.

    Args:
        nodes: Full dependency graph from build_dependency_graph
        unblocking_power: Optional dict of unblocking power scores to display
        filter_states: If set, only show tasks in these states

    Returns:
        ASCII representation of the full DAG
    """
    lines: list[str] = []

    def state_marker(state: str) -> str:
        markers = {
            "done": "✅",
            "cancelled": "❌",
            "active": "🏃",
            "ready_for_review": "👀",
            "waiting": "⏳",
            "todo": "📋",
            "backlog": "📥",
            "blocked": "🚫",
            "external": "🔗",
        }
        return markers.get(state, "❓")

    # Collect non-external nodes
    task_nodes = {n: node for n, node in nodes.items() if not node.is_external}
    if filter_states is not None:
        task_nodes = {n: node for n, node in task_nodes.items() if node.state in filter_states}

    def has_visible_edges(node: DependencyNode) -> bool:
        return any(not dep.is_external and dep.name in task_nodes for dep in node.requires) or any(
            not dep.is_external and dep.name in task_nodes for dep in node.required_by
        )

    def power_label(name: str) -> str:
        if not unblocking_power:
            return ""
        return f" [{unblocking_power.get(name, 0)}↑]"

    # Separate into connected (have visible deps) and isolated (no visible deps)
    connected = {n: node for n, node in task_nodes.items() if has_visible_edges(node)}
    isolated = {n: node for n, node in task_nodes.items() if n not in connected}

    # Render connected tasks: roots first (tasks with no requires of their own)
    rendered: set[str] = set()

    def render_chain(node: DependencyNode, prefix: str, is_last: bool, depth: int) -> None:
        if depth > 8 or node.name in rendered:
            return
        rendered.add(node.name)
        connector = "└── " if is_last else "├── "
        power_str = power_label(node.name)
        lines.append(f"{prefix}{connector}{state_marker(node.state)} {node.name}{power_str}")
        new_prefix = prefix + ("    " if is_last else "│   ")
        deps = [d for d in node.required_by if not d.is_external and d.name in task_nodes]
        for i, dep in enumerate(deps):
            render_chain(dep, new_prefix, i == len(deps) - 1, depth + 1)

    roots = [
        n for n, node in connected.items() if not any(r.name in task_nodes for r in node.requires)
    ]
    for i, root_name in enumerate(sorted(roots)):
        root = connected[root_name]
        power_str = power_label(root_name)
        lines.append(f"{state_marker(root.state)} {root_name}{power_str}")
        deps = [d for d in root.required_by if not d.is_external and d.name in task_nodes]
        for j, dep in enumerate(deps):
            render_chain(dep, "", j == len(deps) - 1, 1)
        if i < len(roots) - 1:
            lines.append("")

    # Render remaining connected tasks not yet shown (non-root connected)
    for name in sorted(connected):
        if name not in rendered and name not in [r for r in roots]:
            node = connected[name]
            power_str = power_label(name)
            lines.append(f"{state_marker(node.state)} {name}{power_str}")

    # Render isolated tasks compactly
    if isolated:
        if lines:
            lines.append("")
        lines.append("── No dependencies ──")
        for name in sorted(isolated):
            node = isolated[name]
            power_str = power_label(name)
            lines.append(f"  {state_marker(node.state)} {name}{power_str}")

    return "\n".join(lines)


def render_tree_ascii(
    root_name: str,
    nodes: dict[str, DependencyNode],
    direction: str = "both",  # "up" (requires), "down" (required_by), "both"
    max_depth: int = 5,
) -> str:
    """Render dependency tree as ASCII art.

    Args:
        root_name: Name of the root task
        nodes: Dependency graph
        direction: Which direction to traverse
        max_depth: Maximum depth to render

    Returns:
        ASCII tree representation
    """
    if root_name not in nodes:
        return f"Task not found: {root_name}"

    root = nodes[root_name]
    lines: list[str] = []
    visited: set[str] = set()

    def state_marker(state: str) -> str:
        """Get state marker for a task."""
        markers = {
            "done": "✅",
            "cancelled": "❌",
            "active": "🏃",
            "ready_for_review": "👀",
            "waiting": "⏳",
            "todo": "📋",
            "backlog": "📥",
            "blocked": "🚫",
            "external": "🔗",
        }
        return markers.get(state, "❓")

    def render_node(
        node: DependencyNode,
        prefix: str,
        is_last: bool,
        depth: int,
        direction_type: str,
    ) -> None:
        if depth > max_depth:
            return

        if node.name in visited:
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{state_marker(node.state)} {node.name} [circular]")
            return

        visited.add(node.name)

        connector = "└── " if is_last else "├── "

        lines.append(f"{prefix}{connector}{state_marker(node.state)} {node.name} ({node.state})")

        new_prefix = prefix + ("    " if is_last else "│   ")

        children: list[tuple[DependencyNode, str]] = []
        if direction_type in ["up", "both"]:
            children.extend((n, "requires") for n in node.requires)
        if direction_type in ["down", "both"]:
            children.extend((n, "required_by") for n in node.required_by)

        for i, (child, _) in enumerate(children):
            is_child_last = i == len(children) - 1
            render_node(child, new_prefix, is_child_last, depth + 1, direction_type)

    # Render header
    lines.append(f"{state_marker(root.state)} {root.name} ({root.state})")

    # Determine if required_by section will be rendered
    has_required_by = direction in ["down", "both"] and root.required_by

    # Render requires section
    if direction in ["up", "both"] and root.requires:
        # Use └── if this is the final section, ├── if required_by follows
        req_connector = "├──" if has_required_by else "└──"
        req_prefix = "│   " if has_required_by else "    "
        lines.append(f"{req_connector} REQUIRES:")
        for i, req in enumerate(root.requires):
            is_last = i == len(root.requires) - 1
            render_node(req, req_prefix, is_last, 1, "up")

    # Render required_by section
    if has_required_by:
        lines.append("└── REQUIRED BY:")
        for i, dep in enumerate(root.required_by):
            is_last = i == len(root.required_by) - 1
            render_node(dep, "    ", is_last, 1, "down")

    return "\n".join(lines)


def render_tree_mermaid(
    root_name: str,
    nodes: dict[str, DependencyNode],
    direction: str = "both",
    max_depth: int = 5,
) -> str:
    """Render dependency tree as Mermaid graph.

    Args:
        root_name: Name of the root task
        nodes: Dependency graph
        direction: Which direction to traverse
        max_depth: Maximum depth to render

    Returns:
        Mermaid graph definition
    """
    if root_name not in nodes:
        return f"%% Task not found: {root_name}"

    lines: list[str] = ["graph TD"]
    edges: set[str] = set()
    visited: set[str] = set()

    def sanitize_id(name: str) -> str:
        """Convert task name to valid Mermaid ID."""
        return name.replace("-", "_").replace("/", "_").replace(":", "_")[:30]

    def add_node(node: DependencyNode, depth: int) -> None:
        if depth > max_depth or node.name in visited:
            return

        visited.add(node.name)
        node_id = sanitize_id(node.name)

        # Add node definition
        lines.append(f'    {node_id}["{node.name}<br/>({node.state})"]')

        # Add edges for requires
        if direction in ["up", "both"]:
            for req in node.requires:
                req_id = sanitize_id(req.name)
                edge = f"    {req_id} --> {node_id}"
                if edge not in edges:
                    edges.add(edge)
                add_node(req, depth + 1)

        # Add edges for required_by
        if direction in ["down", "both"]:
            for dep in node.required_by:
                dep_id = sanitize_id(dep.name)
                edge = f"    {node_id} --> {dep_id}"
                if edge not in edges:
                    edges.add(edge)
                add_node(dep, depth + 1)

    # Build graph
    root = nodes[root_name]
    add_node(root, 0)

    # Add edges
    lines.extend(sorted(edges))

    # Add styling
    lines.extend(
        [
            "",
            "    classDef done fill:#90EE90",
            "    classDef cancelled fill:#FFB6C1",
            "    classDef active fill:#87CEEB",
            "    classDef review fill:#FFD700",
            "    classDef waiting fill:#DDA0DD",
            "    classDef blocked fill:#FF6347",
        ]
    )

    return "\n".join(lines)


def get_dependency_tree(
    task_id: str,
    repo_root: Path,
    format: str = "ascii",  # "ascii" or "mermaid"
    direction: str = "both",
    max_depth: int = 5,
) -> str:
    """Get dependency tree for a task.

    Args:
        task_id: Task ID or name
        repo_root: Repository root path
        format: Output format ("ascii" or "mermaid")
        direction: Which direction to show ("up", "down", "both")
        max_depth: Maximum depth to render

    Returns:
        Formatted dependency tree
    """
    tasks_dir = repo_root / "tasks"
    tasks = load_tasks(tasks_dir)

    # Build graph
    nodes = build_dependency_graph(tasks)

    # Find task by name or ID
    root_name = task_id
    if task_id not in nodes:
        for task in tasks:
            if task.id == task_id:
                root_name = task.name
                break

    # Check for circular dependencies
    cycles = detect_circular_dependencies(nodes)

    # Render tree
    if format == "mermaid":
        tree = render_tree_mermaid(root_name, nodes, direction, max_depth)
    else:
        tree = render_tree_ascii(root_name, nodes, direction, max_depth)

    # Add cycle warnings
    if cycles:
        cycle_strs = [" → ".join(c) for c in cycles]
        warning = "\n⚠️  Circular dependencies detected:\n" + "\n".join(
            f"  • {c}" for c in cycle_strs
        )
        tree += warning

    return tree
