#!/usr/bin/env python3
"""Dependency tree visualization for tasks.

Provides ASCII and Mermaid visualizations of task dependencies,
showing what each task requires and what depends on it.
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
            if req.startswith("http"):
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

    # Render requires section
    if direction in ["up", "both"] and root.requires:
        lines.append("├── REQUIRES:")
        for i, req in enumerate(root.requires):
            is_last = i == len(root.requires) - 1
            render_node(req, "│   ", is_last, 1, "up")

    # Render required_by section
    if direction in ["down", "both"] and root.required_by:
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
