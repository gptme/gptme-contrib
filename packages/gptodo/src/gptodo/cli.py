#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "click>=8.0.0",
#     "rich>=13.0.0",
#     "python-frontmatter>=1.1.0",
#     "tabulate>=0.9.0",
# ]
# [tool.uv]
# exclude-newer = "2024-04-01T00:00:00Z"
# ///

"""Task verification and status CLI for gptme agents.

Features:
- Status views
- Task metadata verification
- Dependency validation
- Link checking
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
)

import click
import frontmatter
from rich.console import Console
from rich.table import Table
from tabulate import tabulate

# Import utilities directly from utils
# Using absolute imports (not relative) for uv script compatibility
from gptodo.utils import (
    # Data classes
    DirectoryConfig,
    TaskInfo,
    # Constants
    CONFIGS,
    STATE_STYLES,
    STATE_EMOJIS,
    # Core utilities
    find_repo_root,
    load_tasks,
    task_to_dict,
    is_task_ready,
    resolve_tasks,
    StateChecker,
    # Auto-unblocking (Issue #206)
    compute_auto_unblock,
    # Phase 4: Effective state computation (bob#240)
    parse_tracking_ref,
    fetch_github_issue_state,
    fetch_github_issue_details,
    fetch_linear_issue_state,
    update_task_state,
    has_new_activity,
    normalize_state,
    # Cache
    get_cache_path,
    load_cache,
    save_cache,
    # URLs
    extract_external_urls,
    fetch_url_state,
)

# Import core business logic from lib
from gptodo.lib import (
    fetch_github_issues,
    fetch_linear_issues,
    generate_task_filename,
    map_priority_from_labels,
    generate_task_content,
)

# Import locking functionality (Phase 3 of Issue #240)
from gptodo.locks import (
    acquire_lock,
    release_lock,
    list_locks,
    cleanup_expired_locks,
    DEFAULT_LOCK_TIMEOUT_HOURS,
)


# Keep console instance for CLI output
console = Console()


@click.group()
@click.option("-v", "--verbose", is_flag=True)
def cli(verbose):
    """Task verification and status CLI."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level)


@cli.command("show")
@click.argument("task_id", required=False)
def show_(task_id):
    """Show detailed information about a task.

    If task_id is not provided, it will show the first task found.
    """
    show(task_id)


def show(task_id):
    """Show detailed information about a task."""
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    if not task_id:
        console.print("[red]Error: Task ID or filename required[/]")
        return

    # Load all tasks
    tasks = load_tasks(tasks_dir)
    if not tasks:
        console.print("[red]No tasks found[/]")
        return

    # Sort tasks by creation date for consistent ID mapping
    tasks.sort(key=lambda t: t.created)

    # Find requested task
    task = None
    if task_id.isdigit():
        # Get task by numeric ID
        idx = int(task_id) - 1
        if 0 <= idx < len(tasks):
            task = tasks[idx]
    else:
        # Get task by name
        task_name = task_id[:-3] if task_id.endswith(".md") else task_id
        matching = [t for t in tasks if t.name == task_name]
        if matching:
            task = matching[0]

    if not task:
        console.print(f"[red]Error: Task {task_id} not found[/]")
        return

    # Create rich table for metadata
    table = Table(show_header=False, box=None)
    table.add_column("Key", style="bold")
    table.add_column("Value")

    # Add metadata rows
    table.add_row("File", str(task.path.relative_to(repo_root)))
    table.add_row("State", task.state or "unknown")
    table.add_row("Created", task.created_ago)
    table.add_row("Modified", task.modified_ago)
    if task.priority:
        table.add_row("Priority", STATE_EMOJIS.get(task.priority) or task.priority)
    if task.tags:
        table.add_row("Tags", ", ".join(task.tags))
    if task.requires:
        table.add_row("Requires", ", ".join(task.requires))
    if task.subtasks.total > 0:
        table.add_row("Subtasks", f"{task.subtasks.completed}/{task.subtasks.total} completed")
    if task.issues:
        table.add_row("Issues", ", ".join(task.issues))

    # Print metadata table
    console.print("\n[bold]Task Metadata:[/]")
    console.print(table)

    # Print content
    console.print("\n[bold]Content:[/]")
    post = frontmatter.load(task.path)  # Reload to get content
    console.out(post.content, highlight=True)


@cli.command("list")
@click.option(
    "--sort",
    type=click.Choice(["state", "date", "name", "completion"]),
    default="date",
    help="Sort by state, creation date, name, or completion percentage",
)
@click.option(
    "--active-only",
    is_flag=True,
    help="Only show new and active tasks",
)
@click.option(
    "--context",
    type=str,
    default=None,
    help="Filter by context tag (e.g., @coding, @research)",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
@click.option(
    "--jsonl",
    "output_jsonl",
    is_flag=True,
    help="Output as JSONL (one task per line) - compact for LLM consumption",
)
def list_(sort, active_only, context, output_json, output_jsonl):
    """List all tasks in a table format."""
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        if output_json:
            print("No tasks found", file=sys.stderr)
            print(json.dumps({"tasks": [], "count": 0}, indent=2))
            return
        if output_jsonl:
            print("No tasks found", file=sys.stderr)
            return  # Empty output for JSONL
        console.print("[red]No tasks found[/]")
        return

    # Create stable enumerated ID mapping based on creation date for ALL tasks
    tasks_by_date = sorted(all_tasks, key=lambda t: t.created)
    name_to_enum_id = {task.name: i for i, task in enumerate(tasks_by_date, 1)}
    # Keep a mapping of all task names for dependency resolution
    all_tasks_dict = {task.name: task for task in all_tasks}

    # Filter tasks if active-only flag is set
    tasks = all_tasks
    if active_only:
        tasks = [task for task in all_tasks if task.state in ["backlog", "active"]]
        if not tasks:
            if output_json:
                print("No new or active tasks found", file=sys.stderr)
                print(json.dumps({"tasks": [], "count": 0}, indent=2))
                return
            if output_jsonl:
                print("No new or active tasks found", file=sys.stderr)
                return
            console.print("[yellow]No new or active tasks found[/]")
            return
        if not output_json and not output_jsonl:
            console.print("[blue]Showing only new and active tasks[/]\n")

    # Filter by context if specified
    if context:
        # Normalize context tag (add @ if missing)
        context_tag = context if context.startswith("@") else f"@{context}"
        tasks = [task for task in tasks if context_tag in (task.tags or [])]
        if not tasks:
            if output_json:
                print(f"No tasks found with context tag '{context_tag}'", file=sys.stderr)
                print(json.dumps({"tasks": [], "count": 0}, indent=2))
                return
            if output_jsonl:
                print(f"No tasks found with context tag '{context_tag}'", file=sys.stderr)
                return
            console.print(f"[yellow]No tasks found with context tag '{context_tag}'[/]")
            return
        if not output_json and not output_jsonl:
            console.print(f"[blue]Showing tasks with context tag '{context_tag}'[/]\n")

    # Sort tasks for display based on option
    if sort == "state":
        tasks.sort(key=lambda t: (t.state or "", t.created))
    elif sort == "name":
        tasks.sort(key=lambda t: t.name)
    elif sort == "completion":
        # Calculate completion percentage, grouping tasks with no subtasks at the bottom
        def completion_key(t):
            if t.subtasks.total == 0:
                return (0, t.created)  # Group at bottom, sort by date within group
            completion_pct = t.subtasks.completed / t.subtasks.total
            return (
                1,
                completion_pct,
                t.created,
            )  # Sort by completion %, newest first within same %

        # Sort in reverse order to get:
        # 1. Tasks with subtasks first (1 > 0)
        # 2. Higher completion percentages first
        # 3. Newer tasks first within same percentage
        tasks.sort(key=completion_key, reverse=True)
    else:  # default: date
        tasks.sort(key=lambda t: t.created)

    # JSONL output - one task per line (compact for LLM consumption)
    if output_jsonl:
        for task in tasks:
            print(json.dumps(task_to_dict(task)))
        return

    # JSON output for machine consumption
    if output_json:
        result = {
            "tasks": [task_to_dict(t) for t in tasks],
            "count": len(tasks),
        }
        print(json.dumps(result, indent=2))
        return

    # Create display rows
    display_rows = []
    for task in tasks:
        # Get stable enumerated ID for task
        enum_id = name_to_enum_id[task.name]

        # Calculate completion info
        if task.subtasks.total > 0:
            completion_pct = (task.subtasks.completed / task.subtasks.total) * 100
            completion_str = f"{completion_pct:>3.0f}%"
            name_with_count = f"{task.name} {task.subtasks}"
        else:
            completion_str = "  -"
            name_with_count = task.name

        # Format dependencies with enumerated IDs or task info
        if task.requires:
            dep_ids = []
            for dep in task.requires:
                if dep in all_tasks_dict:
                    dep_task = all_tasks_dict[dep]
                    # If dependency is in filtered list, show its ID
                    if not active_only or dep_task.state in ["backlog", "active"]:
                        dep_ids.append(str(name_to_enum_id[dep]))
                    else:
                        # Show task name and state for filtered out dependencies
                        state_emoji = STATE_EMOJIS.get(dep_task.state or "untracked", "•")
                        dep_ids.append(f"{dep} ({state_emoji})")
                else:
                    dep_ids.append(f"{dep} (missing)")
            deps_str = ", ".join(dep_ids)
        else:
            deps_str = ""

        # Add row with state emoji
        state_emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
        display_rows.append(
            [
                state_emoji,
                f"{enum_id}. {name_with_count}",
                task.created_ago,
                STATE_EMOJIS.get(task.priority or "") or task.priority or "",
                completion_str,
                deps_str,
            ]
        )

    # Print table
    headers = ["", "Task", "Created", "Priority", "Complete", "Deps"]
    # Only show dependencies column if any task has dependencies
    has_deps = any(task.requires for task in tasks)
    if not has_deps:
        display_rows = [row[:-1] for row in display_rows]
        headers = headers[:-1]

    # Set column alignments and widths
    colaligns = ["left", "left", "left", "center"]
    colwidths = [2, None, None, None]
    if has_deps:
        colaligns.append("left")
        colwidths.append(20)

    console.print(
        "\n"
        + tabulate(
            display_rows,
            headers=headers,
            tablefmt="simple",
            maxcolwidths=colwidths,
            colalign=colaligns,
        )
    )

    # Print legend for tasks with dependencies
    if has_deps:
        tasks_with_deps = [(task, name_to_enum_id[task.name]) for task in tasks if task.requires]
        if tasks_with_deps:
            console.print("\nDependencies:")
            for task, enum_id in tasks_with_deps:
                dep_strs = []
                for dep in task.requires:
                    if dep in all_tasks_dict:
                        dep_task = all_tasks_dict[dep]
                        # If dependency is in filtered list, show its ID
                        if not active_only or dep_task.state in ["backlog", "active"]:
                            dep_strs.append(f"{dep} ({name_to_enum_id[dep]})")
                        else:
                            # Show task name and state for filtered out dependencies
                            state_emoji = STATE_EMOJIS.get(dep_task.state or "untracked", "•")
                            dep_strs.append(f"{dep} ({state_emoji})")
                    else:
                        dep_strs.append(f"{dep} (missing)")
                dep_str = ", ".join(dep_strs)
                console.print(f"  {task.name} ({enum_id}) -> {dep_str}")

    # Print summary
    state_counts: Dict[str, int] = {}
    for task in tasks:
        emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
        state_counts[emoji] = state_counts.get(emoji, 0) + 1

    summary = [f"{count} {state}" for state, count in state_counts.items()]
    console.print(f"\nTotal: {len(tasks)} tasks ({', '.join(summary)})")


def print_status_section(
    console: Console, title: str, items: List[TaskInfo], show_state: bool = False
):
    """Print a section of the status output."""
    if not items:
        return

    # Sort items by creation date (newest first)
    items = sorted(items, key=lambda x: x.created, reverse=True)

    # Get style for this section
    state_name = title.split()[-1].lower()
    style, emoji = STATE_STYLES.get(state_name, ("white", "•"))

    # Limit backlog tasks to 5, show count of remaining
    if state_name == "backlog":
        if len(items) > 5:
            display_items = items[:5]
            remaining = len(items) - 5
        else:
            display_items = items
            remaining = 0
    else:
        display_items = items
        remaining = 0

    # Print header with count and emoji
    emoji = STATE_EMOJIS.get(state_name, "•")
    console.print(f"\n{emoji} {title.upper()} ({len(items)}):")

    # Print items
    for task in display_items:
        # Format display string
        subtask_str = f" {task.subtasks}" if task.subtasks.total > 0 else ""
        priority_str = f" [{task.priority}]" if task.priority else ""

        # Get state info if needed
        state_info = ""
        if show_state:
            # Use "untracked" for None state, with fallback to default style
            state = task.state or "untracked"
            _, state_text = STATE_STYLES.get(state, ("white", "•"))
            state_info = f", {state_text}"

        # Print task info
        console.print(f"  {task.name}{subtask_str}{priority_str} ({task.created_ago}{state_info})")

        # Show issues inline
        if task.issues:
            console.print(f"    ! {', '.join(task.issues)}")

    # Show remaining count for new tasks
    if remaining > 0:
        console.print(f"  ... and {remaining} more")


def print_summary(console: Console, results: Dict[str, List[TaskInfo]], config: DirectoryConfig):
    """Print summary statistics."""
    total = 0
    state_counts: Dict[str, int] = {}

    # Count tasks by state
    for state, items in results.items():
        count = len(items)
        if count > 0:
            total += count
            state_counts[state] = count

    # Build summary strings
    summary_parts = []

    # Add regular states first
    for state in config.states:
        if count := state_counts.get(state, 0):
            style, state_text = STATE_STYLES.get(state, ("white", state))
            emoji = STATE_EMOJIS.get(state, "•")
            summary_parts.append(f"{count} {emoji}")

    # Add special categories
    if count := state_counts.get("untracked", 0):
        summary_parts.append(f"{count} ❓")
    if count := state_counts.get("issues", 0):
        summary_parts.append(f"{count} ⚠️")

    # Print compact summary
    if summary_parts:
        console.print(f"\n{config.emoji} Summary: {total} total ({', '.join(summary_parts)})")


def check_directory(
    console: Console, dir_type: str, repo_root: Path, compact: bool = False
) -> Dict[str, List[TaskInfo]]:
    """Check and display status for a single directory type."""
    config = CONFIGS[dir_type]
    checker = StateChecker(repo_root, config)
    results = checker.check_all()

    # Print header with type-specific color
    style, _ = STATE_STYLES.get(config.states[0], ("white", "•"))
    console.print(f"\n[bold {style}]{config.emoji} {config.type_name.title()} Status[/]\n")

    # Print sections in order
    if results["issues"]:
        print_status_section(
            console,
            "Issues Found",
            results["issues"],
            show_state=True,
        )

    if results["untracked"]:
        print_status_section(
            console,
            "Untracked Files",
            results["untracked"],
        )

    # Determine which states to show based on compact mode
    states_to_show = ["backlog", "active"] if compact else config.states

    # Print active states in order
    for state in states_to_show:
        if state in config.states and results.get(state):
            print_status_section(
                console,
                state,
                results[state],
            )

    # Print summary
    print_summary(console, results, config)

    return results


def print_total_summary(console: Console, all_results: Dict[str, Dict[str, List[TaskInfo]]]):
    """Print summary of all directory types."""
    table = Table(title="\n📊 Total Summary", show_header=False, title_style="bold")
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Details", justify="left")

    total_items = 0
    total_issues = 0

    # Process each directory type
    for dir_type, results in all_results.items():
        config = CONFIGS[dir_type]

        # Calculate totals
        type_total = sum(len(items) for items in results.values())
        type_issues = len(results.get("issues", []))
        total_items += type_total
        total_issues += type_issues

        if type_total == 0:
            continue

        # Build state summary
        state_summary = []
        for state in config.states:
            if count := len(results.get(state, [])):
                emoji = STATE_EMOJIS.get(state, "•")
                state_summary.append(f"{count} {emoji}")

        # Add special categories
        if count := len(results.get("untracked", [])):
            state_summary.append(f"{count} ❓")
        if type_issues:
            state_summary.append(f"{type_issues} ⚠️")

        # Add row to table
        table.add_row(
            config.emoji + " " + config.type_name,
            str(type_total),
            " ".join(state_summary),
        )

    # Add separator and total row
    if total_items > 0:
        table.add_row("", "", "")  # Empty row as separator
        table.add_row(
            "[bold]Total[/]",
            str(total_items),
            f"[yellow]{total_issues} issues[/]" if total_issues else "",
        )

        console.print(table)


@cli.command()
@click.option("--type", type=click.Choice(list(CONFIGS.keys())), default="tasks")
@click.option("--all", is_flag=True, help="Check all directory types")
@click.option("--compact", is_flag=True, help="Only show new and active tasks")
@click.option("--summary", is_flag=True, help="Only show summary")
@click.option("--issues", is_flag=True, help="Only show items with issues")
def status(type, all, compact, summary, issues):
    """Show status of tasks and other tracked items."""
    console = Console()
    repo_root = find_repo_root(Path.cwd())

    # Collect results from all directories
    all_results = {}

    if all:
        # Check all directory types
        for type_name in CONFIGS.keys():
            results = check_directory(console, type_name, repo_root, compact)
            if results:  # Only include directories with items
                all_results[type_name] = results

            # Add separator between types if not last
            if type_name != list(CONFIGS.keys())[-1]:
                console.print("\n" + "─" * 50)

        # Print total summary at the end
        if len(all_results) > 1:
            console.print("\n" + "─" * 50)
            print_total_summary(console, all_results)

    else:
        # Check single directory type
        results = check_directory(console, type, repo_root, compact)
        if results:
            all_results[type] = results

    # Additional filtering based on options
    if issues:
        # Show only items with issues across all types
        has_issues = False
        for dir_type, results in all_results.items():
            if issue_items := results.get("issues", []):
                has_issues = True
                config = CONFIGS[dir_type]
                console.print(f"\n{config.emoji} {dir_type.title()} Issues:")
                for item in issue_items:
                    console.print(f"  • {item.name}: {', '.join(item.issues)}")

        if not has_issues:
            console.print("\n[green]No issues found![/]")

    elif summary:
        # Show only the summary for each type
        for dir_type, results in all_results.items():
            config = CONFIGS[dir_type]
            print_summary(console, results, config)


@cli.command()
@click.option("--fix", is_flag=True, help="Try to fix simple issues")
@click.argument("task_files", nargs=-1, type=click.Path())
def check(fix: bool, task_files: list[str]):
    """Check task integrity and relationships.

    If task files are provided, only check those files.
    Otherwise, check all tasks in the tasks directory.
    """
    console = Console()

    # Find repo root and tasks directory
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # ALWAYS load all tasks to build complete task_ids set for dependency checking
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        console.print("[yellow]No tasks found in tasks directory![/]")
        return

    # Build complete task_ids set from ALL tasks
    task_ids = {task.id for task in all_tasks}

    # Determine which tasks to validate
    if task_files:
        # Only validate the specified files
        tasks_to_validate = []
        for file in task_files:
            path = Path(file)
            # Handle different path formats from pre-commit
            if path.is_absolute():
                # Already absolute, use as-is
                pass
            elif str(path).startswith("tasks/"):
                # Path includes tasks/ prefix, resolve from repo root
                path = repo_root / path
            else:
                # Just filename, resolve from tasks_dir
                path = tasks_dir / path
            try:
                file_tasks = load_tasks(path.parent, single_file=path)
                if file_tasks:
                    tasks_to_validate.extend(file_tasks)
                else:
                    console.print(f"[yellow]Warning: No valid task found in {file}[/]")
            except Exception as e:
                console.print(f"[red]Error reading {file}: {e}[/]")
        if not tasks_to_validate:
            console.print("[yellow]No valid tasks to validate![/]")
            return
    else:
        # Validate all tasks
        tasks_to_validate = all_tasks

    # Track dependencies in tasks being validated
    tasks_with_deps = [task for task in tasks_to_validate if task.requires]

    def has_cycle(task_id: str, visited: Set[str], path: Set[str]) -> bool:
        """Check for circular dependencies."""
        if task_id in path:
            return True
        if task_id in visited:
            return False
        visited.add(task_id)
        path.add(task_id)
        # Find task object to get its dependencies (search in ALL tasks)
        task = next((t for t in all_tasks if t.id == task_id), None)
        if task:
            for dep in task.requires:
                if has_cycle(dep, visited, path):
                    return True
        path.remove(task_id)
        return False

    # Group issues by type
    validation_issues: list[str] = []
    dependency_issues: list[str] = []
    cycle_issues: list[str] = []

    # Collect validation issues from tasks being validated
    for task in tasks_to_validate:
        if task.issues:
            validation_issues.extend(f"{task.id}: {issue}" for issue in task.issues)

    # Check for missing dependencies
    for task in tasks_with_deps:
        for dep in task.requires:
            if dep not in task_ids:
                dependency_issues.append(f"{task.id}: Dependency '{dep}' not found")

    # Check for circular dependencies
    for task in tasks_with_deps:
        if has_cycle(task.id, set(), set()):
            cycle_issues.append(f"Circular dependency detected involving task {task.id}")

    # TODO: Implement link checking
    # for task in tasks:
    #     check_links(task)

    # Report results by category
    has_issues = False

    if validation_issues:
        has_issues = True
        console.print("\n[bold red]Validation Issues:[/]")
        for issue in validation_issues:
            console.print(f"  • {issue}")

    if dependency_issues:
        has_issues = True
        console.print("\n[bold red]Dependency Issues:[/]")
        for issue in dependency_issues:
            console.print(f"  • {issue}")

    if cycle_issues:
        has_issues = True
        console.print("\n[bold red]Circular Dependencies:[/]")
        for issue in cycle_issues:
            console.print(f"  • {issue}")

    if has_issues:
        if fix:
            console.print("\n[yellow]Auto-fix not implemented yet[/]")
            console.print("Suggested fixes:")
            console.print("  • Add missing frontmatter fields")
            console.print("  • Fix invalid state values")
            console.print("  • Update or remove invalid dependencies")
            console.print("  • Break circular dependencies")
        sys.exit(1)
    else:
        total = len(tasks_to_validate)
        with_subtasks = sum(1 for t in tasks_to_validate if t.subtasks.total > 0)
        console.print(
            f"\n[bold green]✓ All {total} tasks verified successfully! "
            f"({with_subtasks} with subtasks)[/]"
        )


# Add priority ranking to the top of the file, after imports
@cli.command("edit")
@click.argument("task_ids", nargs=-1, required=True)
@click.option(
    "--set",
    "set_fields",
    type=(str, str),
    multiple=True,
    help="Set a field value (state, priority, created)",
)
@click.option(
    "--add",
    "add_fields",
    type=(str, str),
    multiple=True,
    help="Add value to a list field (depends, tags)",
)
@click.option(
    "--remove",
    "remove_fields",
    type=(str, str),
    multiple=True,
    help="Remove value from a list field (depends, tags)",
)
@click.option(
    "--set-subtask",
    "set_subtask",
    type=(str, str),
    help="Set subtask state (subtask_text, state). State must be 'done' or 'todo'",
)
@click.option(
    "--auto-unblock/--no-auto-unblock",
    default=True,
    help="Automatically update dependent tasks when marking done (default: enabled)",
)
def edit(task_ids, set_fields, add_fields, remove_fields, set_subtask, auto_unblock):
    """Edit task metadata.

    Examples:
        tasks edit task-123 --set state active
        tasks edit task-123 --set priority high
        tasks edit task-123 --set created 2025-05-05T10:00:00+02:00
        tasks edit task-123 --add depends other-task
        tasks edit task-123 --add tag feature
        tasks edit task-123 --remove tag wip
        tasks edit task-123 --set state active --add tag feature --add depends other-task
        tasks edit task-123 --set-subtask "Handle simple responses" done

    Date formats:
        The created field accepts ISO format dates:
        - Date only: 2025-05-05
        - Date and time: 2025-05-05T10:00:00
        - With timezone: 2025-05-05T10:00:00+02:00
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    tasks = load_tasks(tasks_dir)
    if not tasks:
        console.print("[red]No tasks found[/]")
        return

    # Find tasks to edit
    try:
        target_tasks = resolve_tasks(task_ids, tasks, tasks_dir)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return

    # Validate all field operations before applying any changes
    changes: list[tuple[str, str, str | None]] = []

    # Define valid fields and their validation rules
    VALID_FIELDS: dict[str, dict[str, object]] = {
        # Required fields
        "state": {"type": "enum", "values": CONFIGS["tasks"].states},
        "created": {"type": "date"},
        # Optional fields with validation
        "priority": {"type": "enum", "values": ["high", "medium", "low", "none"]},
        "task_type": {"type": "enum", "values": ["project", "action", "none"]},
        "assigned_to": {"type": "enum", "values": ["agent", "human", "both", "none"]},
        "waiting_since": {"type": "date"},
        # Optional fields with arbitrary string values
        "next_action": {"type": "string"},
        "waiting_for": {"type": "string"},
        "parent": {"type": "string"},  # Parent task ID (for subtasks)
        # List fields handled separately via --add/--remove
        "tags": {"type": "list"},
        "depends": {"type": "list"},  # Deprecated, use requires instead
        "blocks": {"type": "list"},  # Deprecated, use requires instead (note: different semantics)
        "requires": {"type": "list"},  # Required dependencies (canonical)
        "related": {"type": "list"},  # Related items (informational)
        "discovered-from": {"type": "list"},  # Tasks this was discovered from
        "output_types": {"type": "list"},
    }

    # Validate set operations
    for field, value in set_fields:
        # Check if field is valid
        if field not in VALID_FIELDS:
            console.print(
                f"[red]Unknown field: {field}. Valid fields: {', '.join(sorted(VALID_FIELDS.keys()))}[/]"
            )
            return

        field_spec = VALID_FIELDS[field]

        # Handle "none" special value to clear field
        if value == "none":
            changes.append(("set", field, None))
            continue

        # List fields should use --add/--remove instead
        if field_spec["type"] == "list":
            console.print(
                f"[red]Field '{field}' is a list field. Use --add or --remove instead of --set.[/]"
            )
            return

        # Validate based on field type
        if field_spec["type"] == "enum":
            # For state field, normalize deprecated values
            if field == "state":
                normalized = normalize_state(value, warn=True)
                if normalized != value:
                    console.print(
                        f"[yellow]Note: State '{value}' is deprecated, normalizing to '{normalized}'[/]"
                    )
                    value = normalized
            if value not in field_spec["values"]:  # type: ignore[operator]
                valid = ", ".join(field_spec["values"])  # type: ignore[arg-type]
                console.print(f"[red]Invalid {field}: {value}. Valid values: {valid}[/]")
                return
        elif field_spec["type"] == "date":
            try:
                # Parse and validate the date format
                created_dt = datetime.fromisoformat(value)
                # Convert to string format for storage
                value = created_dt.isoformat()
            except ValueError:
                console.print(
                    f"[red]Invalid {field} date format. Use ISO format (YYYY-MM-DD[THH:MM:SS+HH:MM])[/]"
                )
                return
        elif field_spec["type"] == "string":
            # Arbitrary string value - no validation needed
            pass

        changes.append(("set", field, value))

    # Validate subtask operation
    if set_subtask:
        subtask_text, state = set_subtask
        if state not in ["done", "todo"]:
            console.print(f"[red]Invalid subtask state: {state}. Valid values: done, todo[/]")
            return
        changes.append(("set_subtask", subtask_text, state))

    # Canonical list fields in task metadata (no aliases)
    CANONICAL_LIST_FIELDS = {
        "tags",
        "depends",
        "requires",
        "blocks",
        "related",
        "discovered-from",
    }

    # Allowed fields for --add/--remove operations (includes aliases)
    ADDABLE_FIELDS = CANONICAL_LIST_FIELDS | {
        "deps",
        "tag",
        "dep",
        "require",
        "block",
    }

    # Normalize field names (tag -> tags, dep -> depends, deps -> depends, require -> requires, block -> requires, blocks -> requires)
    FIELD_ALIASES = {
        "tag": "tags",
        "dep": "depends",
        "deps": "depends",
        "require": "requires",
        "block": "requires",
        "blocks": "requires",
    }

    # Validate add/remove operations
    for op, fields in [("add", add_fields), ("remove", remove_fields)]:
        for field, value in fields:
            if field not in ADDABLE_FIELDS:
                console.print(
                    f"[red]Cannot {op} to field: {field}. Use --{op} with deps/tags/requires/related/discovered-from.[/]"
                )
                return

            # Warn about blocks → requires semantic change
            if field in ("block", "blocks"):
                console.print(
                    "[yellow]Warning: 'blocks' is deprecated. Use 'requires' instead. "
                    "Note: semantics have changed - 'requires' means dependencies this task needs, "
                    "not tasks that this task blocks.[/]"
                )

            field = FIELD_ALIASES.get(field, field)
            changes.append((op, field, value))

    if not changes:
        console.print("[red]No changes specified. Use --set, --add, or --remove.[/]")
        return

    # Show changes to be made
    console.print("\nChanges to apply:")
    for task in target_tasks:
        task_changes = []

        # Group changes by field for cleaner display
        field_changes: dict[str, list[tuple[str, str | None]]] = {}
        for op, field, value in changes:
            if field not in field_changes:
                field_changes[field] = []
            field_changes[field].append((op, value))

        # Show changes for each field
        for field, field_ops in field_changes.items():
            if field in CANONICAL_LIST_FIELDS:
                current = task.metadata.get(field, [])
                new = current.copy()

                # Apply all operations for this field
                for op, value in field_ops:
                    if op == "add":
                        new = list(set(new + [value]))
                    else:  # remove
                        new = [x for x in new if x != value]

                if new != current:
                    task_changes.append(f"{field}: {', '.join(current)} -> {', '.join(new)}")
            else:
                # For set operations, only show the final value
                set_ops = [v for op, v in field_ops if op == "set"]
                if set_ops:
                    current = task.metadata.get(field)
                    new = set_ops[-1]  # Use the last set value
                    if new != current:
                        task_changes.append(f"{field}: {current} -> {new}")

        if task_changes:
            console.print(f"  {task.name}:")
            for change in task_changes:
                console.print(f"    {change}")

    # Apply changes
    for task in target_tasks:
        post = frontmatter.load(task.path)

        # Apply all changes
        for op, field, value in changes:
            if op == "set_subtask":
                # field is subtask_text, value is state ("done" or "todo")
                subtask_text = field
                state = value

                # Parse markdown body to find and update subtask
                lines = post.content.split("\n")
                updated = False
                for i, line in enumerate(lines):
                    # Check if this line is a subtask checkbox with matching text
                    if subtask_text in line and ("- [ ]" in line or "- [x]" in line):
                        if state == "done":
                            lines[i] = line.replace("- [ ]", "- [x]")
                        else:  # state == "todo"
                            lines[i] = line.replace("- [x]", "- [ ]")
                        updated = True
                        break

                if not updated:
                    console.print(f"[red]Subtask not found: {subtask_text}[/]")
                    return

                post.content = "\n".join(lines)
            elif field in CANONICAL_LIST_FIELDS:
                # Handle list fields (after normalization via FIELD_ALIASES)
                current = post.metadata.get(field, [])
                if op == "add":
                    post.metadata[field] = list(set(current + [value]))
                else:  # remove
                    post.metadata[field] = [x for x in current if x != value]
            else:  # set operation
                if value is None:  # Clear field with "none" value
                    post.metadata.pop(field, None)
                else:
                    # Normalize deprecated states at write time (defense in depth)
                    if field == "state":
                        value = normalize_state(value, warn=False)
                    post.metadata[field] = value

        # Save changes
        with open(task.path, "w") as f:
            f.write(frontmatter.dumps(post))

    # Check if any tasks were marked as done and run completion hook + auto-unblock
    state_changes = [(op, field, value) for op, field, value in changes if field == "state"]
    if any(value == "done" for _, _, value in state_changes):
        # Reload all tasks to get updated state for dependency checking
        all_tasks = load_tasks(tasks_dir)
        all_tasks_dict = {task.name: task for task in all_tasks}

        for task in target_tasks:
            # Re-load task to get updated metadata
            post = frontmatter.load(task.path)
            if post.metadata.get("state") == "done":
                # Run task completion hook if configured via env var
                import subprocess

                hook_cmd = os.environ.get("HOOK_TASK_DONE")
                if hook_cmd:
                    try:
                        subprocess.run([hook_cmd, task.id, task.name, str(repo_root)], check=False)
                    except Exception as e:
                        console.print(f"[yellow]Note: Task completion hook error: {e}[/]")

                # Auto-unblock dependent tasks (Issue #206)
                if auto_unblock:
                    unblocked = compute_auto_unblock(task.name, all_tasks_dict)
                    if unblocked:
                        console.print(
                            f"\n[bold cyan]📋 Auto-unblocking {len(unblocked)} dependent task(s):[/]"
                        )
                        for dep_task in unblocked:
                            # Clear waiting_for if it references the completed task
                            dep_post = frontmatter.load(dep_task.path)
                            waiting_for = dep_post.metadata.get("waiting_for", "")
                            updated_fields = []

                            # Clear waiting_for if it mentions the completed task
                            # Use word boundary match to avoid false positives
                            # (e.g., "task-1" should not match "task-10")
                            if waiting_for and re.search(
                                rf"\b{re.escape(task.name)}\b", waiting_for
                            ):
                                dep_post.metadata.pop("waiting_for", None)
                                dep_post.metadata.pop("waiting_since", None)
                                updated_fields.append("waiting_for")

                            # If task was in 'waiting' state, transition to 'active'
                            if dep_task.state == "waiting":
                                dep_post.metadata["state"] = "active"
                                updated_fields.append("state → active")

                            # Save if any changes were made
                            if updated_fields:
                                with open(dep_task.path, "w") as f:
                                    f.write(frontmatter.dumps(dep_post))
                                console.print(
                                    f"  [green]✓[/] {dep_task.name} (updated: {', '.join(updated_fields)})"
                                )
                            else:
                                console.print(f"  [green]✓[/] {dep_task.name} (now ready)")

    # Show success message
    count = len(target_tasks)
    console.print(f"[green]✓ Updated {count} task{'s' if count > 1 else ''}[/]")


@cli.command("tags")
@click.option("--state", help="Filter by task state")
@click.option("--list", "show_tasks", is_flag=True, help="List tasks for each tag")
@click.argument("filter_tags", nargs=-1)
def tags(state: Optional[str], show_tasks: bool, filter_tags: tuple[str, ...]):
    """List all tags and their task counts.

    Examples:
        tasks tags                    # Show all tags and counts
        tasks tags --list            # Show tags with task lists
        tasks tags --state active    # Only count active tasks
        tasks tags automation ai     # Show specific tags
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    tasks = load_tasks(tasks_dir)
    if not tasks:
        console.print("[yellow]No tasks found![/]")
        return

    # Filter by state if specified
    if state:
        if state not in CONFIGS["tasks"].states:
            console.print(f"[red]Invalid state: {state}[/]")
            return
        tasks = [t for t in tasks if t.state == state]
        if not tasks:
            console.print(f"[yellow]No tasks with state '{state}'[/]")
            return
        console.print(f"[blue]Showing tags for {state} tasks[/]\n")

    # Collect tags and count tasks
    tag_tasks: Dict[str, List[TaskInfo]] = {}
    for task in tasks:
        for tag in task.tags:
            if tag not in tag_tasks:
                tag_tasks[tag] = []
            tag_tasks[tag].append(task)

    if not tag_tasks:
        console.print("[yellow]No tags found![/]")
        return

    # Filter tags if specified
    if filter_tags:
        filtered_tags = {}
        for tag in filter_tags:
            if tag in tag_tasks:
                filtered_tags[tag] = tag_tasks[tag]
            else:
                console.print(f"[yellow]Warning: Tag '{tag}' not found[/]")
        tag_tasks = filtered_tags
        if not tag_tasks:
            console.print("[yellow]No matching tags found![/]")
            return

    # Sort tags by frequency (most used first)
    sorted_tags = sorted(tag_tasks.items(), key=lambda x: (-len(x[1]), x[0]))

    # Print header
    console.print("\n🏷️  Task Tags")

    # Create rows for tabulate
    rows = []
    for tag, tag_task_list in sorted_tags:
        count = len(tag_task_list)
        # Always show tasks if specific tags were requested
        if show_tasks or filter_tags:
            # Sort tasks by state and name
            tag_task_list.sort(key=lambda t: (t.state or "", t.name))
            # Format task list with state emojis
            task_list = []
            for task in tag_task_list:
                emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
                task_list.append(f"{emoji} {task.name}")
            tasks_str = "\n".join(task_list)
            rows.append([tag, str(count), tasks_str])
        else:
            rows.append([tag, str(count)])

    # Print table using tabulate with simple format
    headers = ["Tag", "Count", "Tasks"] if (show_tasks or filter_tags) else ["Tag", "Count"]
    console.print(tabulate(rows, headers=headers, tablefmt="plain"))

    # Print summary
    total_tags = len(sorted_tags)
    total_tasks = len(tasks)
    tagged_tasks = len(set(task.name for tasks in tag_tasks.values() for task in tasks))
    console.print(
        f"\nFound {total_tags} tags across {tagged_tasks} tasks "
        f"({total_tasks - tagged_tasks} untagged)"
    )


@cli.command("ready")
@click.option(
    "--state",
    type=click.Choice(["backlog", "active", "both"]),
    default="both",
    help="Filter by task state (backlog, active, or both)",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
@click.option(
    "--jsonl",
    "output_jsonl",
    is_flag=True,
    help="Output as JSONL (one task per line) - compact for LLM consumption",
)
@click.option(
    "--use-cache",
    "use_cache",
    is_flag=True,
    help="Check URL-based requires against cached states (run 'fetch' first)",
)
def ready(state, output_json, output_jsonl, use_cache):
    """List all ready (unblocked) tasks.

    Shows tasks that have no dependencies or whose dependencies are all completed.
    Inspired by beads' `bd ready` command for finding work to do.

    Use --json for machine-readable output in autonomous workflows.
    Use --jsonl for compact one-task-per-line output (better with head -n).
    Use --use-cache to also check URL-based 'requires' against cached issue states.
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        if output_json:
            print("No tasks found", file=sys.stderr)
            print(json.dumps({"ready_tasks": [], "count": 0}, indent=2))
            return
        if output_jsonl:
            print("No tasks found", file=sys.stderr)
            return  # Empty output for JSONL
        console.print("[yellow]No tasks found![/]")
        return

    # Create task lookup dictionary
    tasks_dict = {task.name: task for task in all_tasks}

    # Load cache if requested
    issue_cache: Optional[Dict[str, Any]] = None
    if use_cache:
        cache_path = get_cache_path(repo_root)
        issue_cache = load_cache(cache_path)
        if not issue_cache:
            console.print("[yellow]Warning: Cache empty. Run 'gptodo fetch' first.[/]")

    # Filter by state first
    if state == "backlog":
        filtered_tasks = [task for task in all_tasks if task.state == "backlog"]
    elif state == "active":
        filtered_tasks = [task for task in all_tasks if task.state == "active"]
    else:  # both
        filtered_tasks = [task for task in all_tasks if task.state in ["backlog", "active"]]

    # Filter for ready (unblocked) tasks
    ready_tasks = [task for task in filtered_tasks if is_task_ready(task, tasks_dict, issue_cache)]

    if not ready_tasks:
        if output_json:
            print("No ready tasks found", file=sys.stderr)
            print(json.dumps({"ready_tasks": [], "count": 0}, indent=2))
            return
        if output_jsonl:
            print("No ready tasks found", file=sys.stderr)
            return  # Empty output for JSONL
        console.print("[yellow]No ready tasks found![/]")
        console.print("\n[dim]Tip: Try checking blocked tasks with --state to see dependencies[/]")
        return

    # Sort by priority (high to low) and then by creation date (oldest first)
    ready_tasks.sort(
        key=lambda t: (
            -t.priority_rank,
            t.created,
        )
    )

    # JSONL output - one task per line (compact for LLM consumption)
    if output_jsonl:
        for task in ready_tasks:
            print(json.dumps(task_to_dict(task)))
        return

    # JSON output for machine consumption
    if output_json:
        result = {
            "ready_tasks": [task_to_dict(t) for t in ready_tasks],
            "count": len(ready_tasks),
        }
        print(json.dumps(result, indent=2))
        return

    # Create table
    table = Table(title=f"[bold green]Ready Tasks[/] ({len(ready_tasks)} unblocked)")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("State", style="blue")
    table.add_column("Priority", style="yellow")
    table.add_column("Task", style="white")
    table.add_column("Subtasks", style="magenta")
    table.add_column("Activity", style="green")  # New activity indicator

    # Create stable enumerated ID mapping
    tasks_by_date = sorted(all_tasks, key=lambda t: t.created)
    name_to_enum_id = {task.name: i for i, task in enumerate(tasks_by_date, 1)}

    for task in ready_tasks:
        enum_id = name_to_enum_id[task.name]
        state_emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
        priority_emoji = STATE_EMOJIS.get(task.priority or "", "")

        if task.subtasks.total > 0:
            subtasks_str = f"{task.subtasks.completed}/{task.subtasks.total}"
        else:
            subtasks_str = "-"

        # Check for new activity on tracked URLs (Issue #241)
        activity_str = "-"
        if use_cache and issue_cache:
            tracking = task.metadata.get("tracking")
            waiting_since = task.metadata.get("waiting_since")
            if tracking and waiting_since:
                # Handle tracking as list or string
                tracking_urls = tracking if isinstance(tracking, list) else [tracking]
                for url in tracking_urls:
                    cached = issue_cache.get(url)
                    if cached and has_new_activity(cached.get("updatedAt"), waiting_since):
                        activity_str = "[bold green]🔔 NEW[/]"
                        break

        table.add_row(
            str(enum_id),
            state_emoji,
            priority_emoji or (task.priority or ""),
            task.name,
            subtasks_str,
            activity_str,
        )

    console.print(table)
    console.print("\n[dim]Run [bold]gptodo next[/] to pick the top priority ready task[/]")


@cli.command("next")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
@click.option(
    "--use-cache",
    "use_cache",
    is_flag=True,
    help="Check URL-based requires against cached states (run 'fetch' first)",
)
def next_(output_json, use_cache):
    """Show the highest priority ready (unblocked) task.

    Picks from new or active tasks that have no dependencies
    or whose dependencies are all completed.

    Use --json for machine-readable output in autonomous workflows.
    Use --use-cache to also check URL-based 'requires' against cached issue states.
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        if output_json:
            print("No tasks found", file=sys.stderr)
            print(
                json.dumps(
                    {"next_task": None, "alternatives": [], "error": "No tasks found"},
                    indent=2,
                )
            )
            return
        console.print("[yellow]No tasks found![/]")
        return

    # Create task lookup dictionary
    tasks_dict = {task.name: task for task in all_tasks}

    # Load cache if requested
    issue_cache: Optional[Dict[str, Any]] = None
    if use_cache:
        cache_path = get_cache_path(repo_root)
        issue_cache = load_cache(cache_path)

    # Filter for new or active tasks
    workable_tasks = [task for task in all_tasks if task.state in ["backlog", "active"]]
    if not workable_tasks:
        if output_json:
            print("No new or active tasks found", file=sys.stderr)
            print(
                json.dumps(
                    {
                        "next_task": None,
                        "alternatives": [],
                        "error": "No new or active tasks found",
                    },
                    indent=2,
                )
            )
            return
        console.print("[yellow]No new or active tasks found![/]")
        return

    # Filter for ready (unblocked) tasks
    ready_tasks = [task for task in workable_tasks if is_task_ready(task, tasks_dict, issue_cache)]

    if not ready_tasks:
        if output_json:
            print("No ready tasks found", file=sys.stderr)
            print(
                json.dumps(
                    {
                        "next_task": None,
                        "alternatives": [],
                        "error": "No ready tasks found",
                    },
                    indent=2,
                )
            )
            return
        console.print("[yellow]No ready tasks found![/]")
        console.print("\n[dim]All new/active tasks are blocked by dependencies.[/]")
        console.print("[dim]Run [bold]gptodo ready --state both[/] to see all ready work[/]")
        return

    # Sort tasks by priority (high to low) and then by creation date (oldest first)
    ready_tasks.sort(
        key=lambda t: (
            -t.priority_rank,
            t.created,
        )
    )

    # Get the highest priority ready task
    next_task = ready_tasks[0]

    # JSON output for machine consumption
    if output_json:
        result = {
            "next_task": task_to_dict(next_task),
            "alternatives": [task_to_dict(t) for t in ready_tasks[1:4]],  # Top 3 alternatives
        }
        print(json.dumps(result, indent=2))
        return

    # Show task using same format as show command
    console.print(f"\n[bold blue]🏃 Next Task:[/] (Priority: {next_task.priority or 'none'})")
    # Call show command directly instead of using callback
    show(next_task.name)


@cli.command("stale")
@click.option(
    "--days",
    default=30,
    type=int,
    help="Number of days without modification to consider stale (default: 30)",
)
@click.option(
    "--state",
    type=click.Choice(["active", "backlog", "waiting", "all"]),
    default="active",
    help="Filter by task state (default: active)",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
@click.option(
    "--jsonl",
    "output_jsonl",
    is_flag=True,
    help="Output as JSONL (one task per line) - compact for LLM consumption",
)
def stale(days: int, state: str, output_json: bool, output_jsonl: bool):
    """List stale tasks that haven't been modified recently.

    Identifies tasks that may need review for completion, archival, or reassessment.
    By default shows active tasks not modified in 30+ days.

    Examples:
        gptodo stale                    # Active tasks unchanged for 30+ days
        gptodo stale --days 60          # Active tasks unchanged for 60+ days
        gptodo stale --state all        # All tasks regardless of state
        gptodo stale --state paused     # Only paused stale tasks
        gptodo stale --json             # Machine-readable output
        gptodo stale --jsonl            # One task per line (LLM-friendly)
    """
    console = Console()

    # Validate days parameter
    if days <= 0:
        if output_json:
            print(json.dumps({"error": "--days must be a positive integer"}, indent=2))
            raise SystemExit(1)
        console.print("[red]Error: --days must be a positive integer[/]")
        raise SystemExit(1)

    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        if output_json:
            print("No tasks found", file=sys.stderr)
            print(json.dumps({"stale_tasks": [], "count": 0}, indent=2))
            return
        if output_jsonl:
            print("No tasks found", file=sys.stderr)
            return  # Empty output for JSONL
        console.print("[yellow]No tasks found![/]")
        return

    # Calculate cutoff date
    cutoff = datetime.now() - timedelta(days=days)

    # Filter by state
    if state == "all":
        state_filtered = all_tasks
    else:
        state_filtered = [task for task in all_tasks if task.state == state]

    # Filter for stale tasks (not modified since cutoff)
    stale_tasks = [task for task in state_filtered if task.modified < cutoff]

    if not stale_tasks:
        if output_json:
            print(
                f"No stale tasks found (threshold: {days} days, state: {state})",
                file=sys.stderr,
            )
            print(json.dumps({"stale_tasks": [], "count": 0, "days_threshold": days}, indent=2))
            return
        if output_jsonl:
            print(
                f"No stale tasks found (threshold: {days} days, state: {state})",
                file=sys.stderr,
            )
            return  # Empty output for JSONL when no tasks
        console.print(f"[green]No stale tasks found![/] (threshold: {days} days, state: {state})")
        return

    # Sort by modification date (oldest first)
    stale_tasks.sort(key=lambda t: t.modified)

    # JSONL output - one task per line (compact for LLM consumption)
    if output_jsonl:
        for task in stale_tasks:
            task_dict = task_to_dict(task)
            task_dict["days_since_modified"] = (datetime.now() - task.modified).days
            print(json.dumps(task_dict))
        return

    # JSON output for machine consumption
    if output_json:
        result = {
            "stale_tasks": [
                {
                    **task_to_dict(t),
                    "days_since_modified": (datetime.now() - t.modified).days,
                }
                for t in stale_tasks
            ],
            "count": len(stale_tasks),
            "days_threshold": days,
            "state_filter": state,
        }
        print(json.dumps(result, indent=2))
        return

    # Create table
    table = Table(
        title=f"[bold yellow]Stale Tasks[/] ({len(stale_tasks)} unchanged for {days}+ days)"
    )
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("State", style="blue")
    table.add_column("Days Stale", style="red", justify="right")
    table.add_column("Task", style="white")
    table.add_column("Progress", style="magenta")
    table.add_column("Priority", style="yellow")

    # Create stable enumerated ID mapping
    tasks_by_date = sorted(all_tasks, key=lambda t: t.created)
    name_to_enum_id = {task.name: i for i, task in enumerate(tasks_by_date, 1)}

    for task in stale_tasks:
        enum_id = name_to_enum_id[task.name]
        state_emoji = STATE_EMOJIS.get(task.state or "untracked", "•")
        priority_emoji = STATE_EMOJIS.get(task.priority or "", "")
        days_stale = (datetime.now() - task.modified).days

        # Progress display
        if task.subtasks.total > 0:
            pct = int(task.subtasks.completed / task.subtasks.total * 100)
            progress_str = f"{pct}% ({task.subtasks.completed}/{task.subtasks.total})"
        else:
            progress_str = "-"

        table.add_row(
            str(enum_id),
            state_emoji,
            str(days_stale),
            task.name,
            progress_str,
            priority_emoji or (task.priority or "-"),
        )

    console.print(table)
    console.print("\n[dim]Review these tasks for: completion, archival, or reassessment[/]")
    console.print("[dim]Run [bold]gptodo show <id>[/] to inspect a task's details[/]")


@cli.command("sync")
@click.option(
    "--update",
    is_flag=True,
    help="Update task states to match GitHub issue states",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
@click.option(
    "--use-cache",
    "use_cache",
    is_flag=True,
    help="Use cached issue states instead of live API calls (run 'fetch' first)",
)
@click.option(
    "--light",
    is_flag=True,
    help="Light sync: poll notifications to invalidate stale cache, then sync",
)
@click.option(
    "--full",
    is_flag=True,
    help="Full sync: refresh all cached URLs regardless of age, then sync",
)
def sync(update, output_json, use_cache, light, full):
    """Sync task states with linked GitHub issues.

    Finds tasks with tracking field in frontmatter and compares
    their state with the linked GitHub issue state.

    Sync modes (Phase 4 - bob#240):
    --light: Poll GitHub notifications, invalidate relevant caches, refresh
             affected URLs. Fast check for recent changes.
    --full:  Refresh ALL external URLs regardless of cache age. Slower but
             ensures complete cache freshness.

    Use --update to automatically update task states to match issue states.
    Use --json for machine-readable output.
    Use --use-cache to check cached states (faster, run 'fetch' first).

    GitHub state mapping:
    - OPEN issue -> task should be active/new
    - CLOSED issue -> task should be done
    """
    from .lib import poll_github_notifications, extract_urls_from_notification

    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"
    cache_path = get_cache_path(repo_root)

    # Handle light/full sync modes (Phase 4 - bob#240)
    if light and full:
        console.print("[red]Error: Cannot use --light and --full together[/]")
        return

    if light:
        # Light sync: Poll notifications to invalidate stale cache entries
        console.print("[cyan]Light sync: Polling GitHub notifications...[/]")
        cache = load_cache(cache_path)

        notifications = poll_github_notifications()
        if notifications:
            urls_to_refresh: set[str] = set()
            for notif in notifications:
                urls = extract_urls_from_notification(notif)
                urls_to_refresh.update(urls)

            # Invalidate cache entries for URLs with notifications
            invalidated = 0
            for url in urls_to_refresh:
                if url in cache:
                    del cache[url]
                    invalidated += 1

            if invalidated > 0:
                console.print(f"[yellow]Invalidated {invalidated} cache entries[/]")
                save_cache(cache_path, cache)

            # Fetch fresh states for invalidated URLs
            if urls_to_refresh:
                console.print(f"[cyan]Refreshing {len(urls_to_refresh)} URLs...[/]")
                for url in urls_to_refresh:
                    state_info = fetch_url_state(url)
                    if state_info:
                        cache[url] = {
                            "state": state_info["state"],
                            "source": state_info.get("source", "unknown"),
                            "last_fetched": datetime.now(timezone.utc).isoformat(),
                            "updatedAt": state_info.get("updatedAt"),
                        }
                save_cache(cache_path, cache)
        else:
            console.print("[dim]No new notifications found[/]")

        # Continue with sync using updated cache
        use_cache = True

    if full:
        # Full sync: Refresh ALL external URLs
        console.print("[cyan]Full sync: Refreshing all external URLs...[/]")
        all_tasks_for_scan = load_tasks(tasks_dir)

        all_urls: set[str] = set()
        for task in all_tasks_for_scan:
            task_urls = extract_external_urls(task)
            all_urls.update(task_urls)

        if all_urls:
            console.print(f"[cyan]Fetching {len(all_urls)} URLs...[/]")
            cache = load_cache(cache_path)

            fetched = 0
            for url in sorted(all_urls):
                state_info = fetch_url_state(url)
                if state_info:
                    cache[url] = {
                        "state": state_info["state"],
                        "source": state_info.get("source", "unknown"),
                        "last_fetched": datetime.now(timezone.utc).isoformat(),
                        "updatedAt": state_info.get("updatedAt"),
                    }
                    fetched += 1

            save_cache(cache_path, cache)
            console.print(f"[green]Refreshed {fetched} URLs[/]")

        # Continue with sync using refreshed cache
        use_cache = True

    # Load cache if requested (or if light/full already set it)
    cache_loaded = "cache" in dir() and cache  # Check if cache was already loaded
    if use_cache and not cache_loaded:
        cache = load_cache(cache_path)
        if not cache:
            console.print("[yellow]Warning: Cache is empty. Run 'gptodo fetch' first.[/]")
    elif not use_cache:
        cache = {}

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        console.print("[yellow]No tasks found![/]")
        return

    # Find tasks with tracking field
    tasks_with_tracking = []
    for task in all_tasks:
        tracking = task.metadata.get("tracking")
        if tracking:
            # Handle tracking as either list or string for compatibility
            tracking_urls = tracking if isinstance(tracking, list) else [tracking]
            for tracking_url in tracking_urls:
                tasks_with_tracking.append((task, tracking_url))

    if not tasks_with_tracking:
        if output_json:
            print(
                json.dumps(
                    {
                        "synced_tasks": [],
                        "count": 0,
                        "message": "No tasks with tracking field found",
                    },
                    indent=2,
                )
            )
            return
        console.print("[yellow]No tasks with tracking field found![/]")
        console.print(
            "\n[dim]Add tracking to frontmatter: tracking: 'https://github.com/owner/repo/issues/123'[/]"
        )
        return

    # Check each task against GitHub
    results = []
    for task, tracking_ref in tasks_with_tracking:
        # Parse tracking reference (supports owner/repo#123 or full URL)
        issue_info = parse_tracking_ref(tracking_ref)
        if not issue_info:
            results.append(
                {
                    "task": task.name,
                    "tracking": tracking_ref,
                    "error": "Could not parse tracking reference",
                    "task_state": task.state,
                    "issue_state": None,
                    "in_sync": False,
                }
            )
            continue

        # Fetch issue state (from cache or live API)
        issue_state = None
        source = issue_info.get("source", "github")

        if source == "github":
            if use_cache:
                # Try to find state in cache using URL format
                # Construct URL from parsed info
                issue_type = "issues"  # Default to issues
                cache_url = (
                    f"https://github.com/{issue_info['repo']}/{issue_type}/{issue_info['number']}"
                )
                cached = cache.get(cache_url)

                # Also try pull request URL
                if not cached:
                    cache_url = (
                        f"https://github.com/{issue_info['repo']}/pull/{issue_info['number']}"
                    )
                    cached = cache.get(cache_url)

                if cached:
                    issue_state = cached.get("state")

            if issue_state is None and not use_cache:
                # Fetch from GitHub API
                issue_state = fetch_github_issue_state(issue_info["repo"], issue_info["number"])

        elif source == "linear":
            identifier = issue_info.get("identifier", "")
            team = issue_info.get("team", "")

            if use_cache:
                # Try to find state in cache using Linear URL format
                cache_url = f"https://linear.app/{team}/issue/{identifier}"
                cached = cache.get(cache_url)
                if cached:
                    # Linear states need normalization to OPEN/CLOSED
                    raw_state = cached.get("state")
                    if raw_state in ("completed", "canceled"):
                        issue_state = "CLOSED"
                    elif raw_state:
                        issue_state = "OPEN"

            if issue_state is None and not use_cache:
                # Fetch from Linear API
                raw_state = fetch_linear_issue_state(identifier)
                if raw_state:
                    # Normalize Linear states to OPEN/CLOSED
                    if raw_state in ("completed", "canceled"):
                        issue_state = "CLOSED"
                    else:
                        issue_state = "OPEN"

        if issue_state is None:
            source_name = "Linear" if source == "linear" else "GitHub"
            error_msg = (
                "Issue not in cache (run 'gptodo fetch' first)"
                if use_cache
                else f"Could not fetch issue state from {source_name}"
            )
            results.append(
                {
                    "task": task.name,
                    "tracking": tracking_ref,
                    "error": error_msg,
                    "task_state": task.state,
                    "issue_state": None,
                    "in_sync": False,
                }
            )
            continue

        # Determine expected task state based on issue state
        expected_state = "done" if issue_state == "CLOSED" else (task.state or "active")
        if issue_state == "OPEN" and task.state == "done":
            expected_state = "active"  # Reopened issue

        in_sync = (issue_state == "CLOSED" and task.state == "done") or (
            issue_state == "OPEN" and task.state in ["backlog", "active", "waiting"]
        )

        # Check for new activity since waiting_since (Issue #241 feature)
        updated_at = None
        new_activity = False
        waiting_since = task.metadata.get("waiting_since")

        if source == "github":
            if use_cache:
                cached = cache.get(cache_url)  # Cache keyed by full URL
                if cached:
                    updated_at = cached.get("updatedAt")
            else:
                # Fetch details including updatedAt
                details = fetch_github_issue_details(issue_info["repo"], issue_info["number"])
                if details:
                    updated_at = details.get("updatedAt")

        # Check if there's new activity since waiting_since
        if waiting_since and updated_at:
            new_activity = has_new_activity(updated_at, waiting_since)

        result = {
            "task": task.name,
            "tracking": tracking_ref,
            "task_state": task.state,
            "issue_state": issue_state,
            "expected_state": expected_state,
            "in_sync": in_sync,
            "updated_at": updated_at,
            "waiting_since": str(waiting_since) if waiting_since else None,
            "new_activity": new_activity,
        }

        # Update task if requested and out of sync
        if update and not in_sync:
            if update_task_state(task.path, expected_state):
                result["updated"] = True
                result["new_state"] = expected_state
            else:
                result["error"] = "Failed to update task file"

        results.append(result)

    # Output results
    if output_json:
        output = {
            "synced_tasks": results,
            "count": len(results),
            "in_sync": sum(1 for r in results if r.get("in_sync", False)),
            "out_of_sync": sum(1 for r in results if not r.get("in_sync", False)),
            "with_new_activity": sum(1 for r in results if r.get("new_activity", False)),
        }
        if update:
            output["updated"] = sum(1 for r in results if r.get("updated", False))
        print(json.dumps(output, indent=2))
        return

    # Rich table output
    table = Table(title=f"[bold]Task-Issue Sync Status[/] ({len(results)} tracked)")
    table.add_column("Task", style="cyan")
    table.add_column("Issue", style="blue")
    table.add_column("Task State", style="yellow")
    table.add_column("Issue State", style="green")
    table.add_column("Activity", style="magenta")
    table.add_column("Status", style="white")

    for result in results:
        if result.get("error"):
            status = f"[red]Error: {result['error']}[/]"
        elif result.get("in_sync"):
            status = "[green]✓ In sync[/]"
        elif result.get("updated"):
            status = f"[blue]→ Updated to {result['new_state']}[/]"
        else:
            status = (
                f"[yellow]⚠ Out of sync (expected: {result.get('expected_state', 'unknown')})[/]"
            )

        # Activity indicator - shows if there's new activity since waiting_since
        if result.get("new_activity"):
            activity = "[bold green]🔔 NEW[/]"
        elif result.get("waiting_since"):
            activity = f"[dim]since {result['waiting_since']}[/]"
        else:
            activity = "[dim]—[/]"

        table.add_row(
            result["task"][:30],
            result.get("tracking", "")[:25],
            result.get("task_state", ""),
            result.get("issue_state", "N/A"),
            activity,
            status,
        )

    console.print(table)

    out_of_sync = sum(1 for r in results if not r.get("in_sync", False) and not r.get("error"))
    if out_of_sync > 0 and not update:
        console.print(f"\n[dim]Run with --update to sync {out_of_sync} out-of-sync tasks[/]")


@cli.command()
@click.argument("task_id")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
def plan(task_id: str, output_json: bool):
    """Show the impact of completing a task.

    Analyzes what tasks would be unblocked if the specified task is completed.
    Useful for prioritizing work based on impact.

    The TASK_ID can be the numeric ID or task name.

    Examples:
        gptodo plan 5                   # Impact analysis for task #5
        gptodo plan my-task-name        # By task name
        gptodo plan 5 --json            # Machine-readable output
    """
    console = Console()

    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Load all tasks
    all_tasks = load_tasks(tasks_dir)
    if not all_tasks:
        if output_json:
            print(json.dumps({"error": "No tasks found"}, indent=2))
            raise SystemExit(1)
        console.print("[red]No tasks found![/]")
        raise SystemExit(1)

    # Create stable enumerated ID mapping
    tasks_by_date = sorted(all_tasks, key=lambda t: t.created)
    name_to_enum_id = {task.name: i for i, task in enumerate(tasks_by_date, 1)}
    enum_id_to_task = {i: task for i, task in enumerate(tasks_by_date, 1)}

    # Resolve task_id to actual task
    target_task = None

    # Try as numeric ID first
    try:
        numeric_id = int(task_id)
        if numeric_id in enum_id_to_task:
            target_task = enum_id_to_task[numeric_id]
    except ValueError:
        pass

    # Try as task name
    if target_task is None:
        for task in all_tasks:
            if task.name == task_id or task.name == task_id.replace("-", "_"):
                target_task = task
                break

    if target_task is None:
        if output_json:
            print(json.dumps({"error": f"Task '{task_id}' not found"}, indent=2))
            raise SystemExit(1)
        console.print(f"[red]Task '{task_id}' not found![/]")
        raise SystemExit(1)

    # Find all tasks that depend on this task (reverse dependency lookup)
    dependent_tasks = []
    for task in all_tasks:
        if target_task.name in task.requires:
            dependent_tasks.append(task)

    # Calculate impact score
    # Scoring:
    # - Base: 1 point per dependent task
    # - Priority bonus: high=3, medium=2, low=1
    # - State bonus: active=2, backlog=1 (these would benefit most from unblocking)
    impact_score = 0.0
    priority_weights = {"high": 3, "medium": 2, "low": 1}
    state_weights = {"active": 2, "backlog": 1}

    impact_details = []
    for dep_task in dependent_tasks:
        task_impact = 1.0  # Base score
        task_impact += priority_weights.get(dep_task.priority or "", 0)
        task_impact += state_weights.get(dep_task.state or "", 0)
        impact_score += task_impact
        impact_details.append(
            {
                "task": dep_task.name,
                "state": dep_task.state or "unknown",
                "priority": dep_task.priority or "none",
                "impact_contribution": task_impact,
            }
        )

    # Check if this task has unmet dependencies itself
    unmet_dependencies = []
    for dep_name in target_task.requires:
        for task in all_tasks:
            if task.name == dep_name and task.state not in ["done", "cancelled"]:
                unmet_dependencies.append({"task": task.name, "state": task.state or "unknown"})
                break

    # JSON output
    if output_json:
        result = {
            "task": target_task.name,
            "task_id": name_to_enum_id[target_task.name],
            "state": target_task.state or "unknown",
            "priority": target_task.priority or "none",
            "impact_analysis": {
                "score": round(impact_score, 1),
                "would_unblock": len(dependent_tasks),
                "dependent_tasks": impact_details,
            },
            "blockers": {
                "has_unmet_dependencies": len(unmet_dependencies) > 0,
                "unmet_dependencies": unmet_dependencies,
            },
        }
        print(json.dumps(result, indent=2))
        return

    # Rich console output
    target_id = name_to_enum_id[target_task.name]
    state_emoji = STATE_EMOJIS.get(target_task.state or "untracked", "•")

    console.print(f"\n[bold cyan]Impact Analysis: {target_task.name}[/] (#{target_id})")
    console.print(f"State: {state_emoji} {target_task.state or 'unknown'}")
    console.print(f"Priority: {target_task.priority or 'none'}")

    # Show unmet dependencies (blockers for this task)
    if unmet_dependencies:
        console.print(f"\n[bold red]⚠ Blocked by {len(unmet_dependencies)} unmet dependencies:[/]")
        for dep in unmet_dependencies:
            dep_emoji = STATE_EMOJIS.get(dep["state"], "•")
            console.print(f"  - {dep_emoji} {dep['task']} ({dep['state']})")

    # Show what would be unblocked
    console.print(f"\n[bold green]Impact Score: {impact_score:.1f}[/]")

    if dependent_tasks:
        console.print(
            f"\n[bold]Completing this task would unblock {len(dependent_tasks)} task(s):[/]"
        )

        table = Table()
        table.add_column("Task", style="white")
        table.add_column("State", style="blue")
        table.add_column("Priority", style="yellow")
        table.add_column("Impact", style="green", justify="right")

        for detail in impact_details:
            task_emoji = STATE_EMOJIS.get(str(detail["state"]), "•")
            table.add_row(
                str(detail["task"]),
                f"{task_emoji} {detail['state']}",
                str(detail["priority"]),
                f"+{detail['impact_contribution']:.1f}",
            )

        console.print(table)
    else:
        console.print("\n[dim]No tasks depend on this task (leaf node).[/]")

    # Summary recommendation
    console.print()
    if impact_score >= 5:
        console.print(
            "[bold green]High impact![/] Completing this task will significantly unblock progress."
        )
    elif impact_score >= 2:
        console.print(
            "[yellow]Moderate impact.[/] Consider prioritizing if other high-impact tasks are blocked."
        )
    else:
        console.print("[dim]Low impact.[/] This is a leaf task or has few dependents.")


# ============================================================================
# Fetch Command - Cache external issue states
# ============================================================================


@cli.command("fetch")
@click.option(
    "--all",
    "fetch_all",
    is_flag=True,
    help="Refresh all URLs ignoring cache age",
)
@click.option(
    "--max-age",
    type=click.IntRange(min=0),
    default=3600,
    help="Max cache age in seconds before refetch (default: 3600, must be >= 0)",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
@click.argument("urls", nargs=-1)
def fetch(fetch_all: bool, max_age: int, output_json: bool, urls: tuple[str, ...]):
    """Fetch and cache external issue/PR states.

    Retrieves state (open/closed) from GitHub URLs and caches locally.
    This enables fast state checks in sync/ready commands without API calls.

    By default, scans all tasks for external URLs in tracking, requires,
    and related fields. Pass explicit URLs to fetch specific items.

    Cache is stored in state/issue-cache.json.

    Examples:
        gptodo fetch                              # Fetch all external URLs from tasks
        gptodo fetch --all                        # Refresh all (ignore cache age)
        gptodo fetch https://github.com/o/r/issues/1  # Fetch specific URL
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    cache_path = get_cache_path(repo_root)
    cache = load_cache(cache_path)
    now = datetime.now(timezone.utc)

    # Collect URLs to fetch
    urls_to_fetch: Set[str] = set()

    if urls:
        # Explicit URLs provided
        urls_to_fetch.update(urls)
    else:
        # Scan tasks for external URLs
        tasks_dir = repo_root / "tasks"
        all_tasks = load_tasks(tasks_dir)

        for task in all_tasks:
            task_urls = extract_external_urls(task)
            urls_to_fetch.update(task_urls)

    if not urls_to_fetch:
        if output_json:
            print(json.dumps({"fetched": 0, "cached": 0, "results": []}, indent=2))
            return
        console.print("[yellow]No external URLs found in tasks.[/]")
        console.print("\n[dim]Add tracking, requires, or related URLs to task frontmatter.[/]")
        return

    # Filter by cache age if not --all
    if not fetch_all:
        fresh_urls = set()
        for url in urls_to_fetch:
            cached = cache.get(url)
            if cached:
                try:
                    cached_time = datetime.fromisoformat(
                        cached.get("last_fetched", "1970-01-01T00:00:00")
                    )
                    # Handle timezone-naive cached times by assuming UTC
                    if cached_time.tzinfo is None:
                        cached_time = cached_time.replace(tzinfo=timezone.utc)
                    age_seconds = (now - cached_time).total_seconds()
                    if age_seconds < max_age:
                        fresh_urls.add(url)
                except (ValueError, TypeError):
                    # Malformed cache entry - treat as stale
                    pass

        stale_urls = urls_to_fetch - fresh_urls
    else:
        stale_urls = urls_to_fetch
        fresh_urls = set()

    # Fetch stale URLs
    results = []
    fetched_count = 0
    error_count = 0

    for url in sorted(stale_urls):
        result = {"url": url}
        state_info = fetch_url_state(url)

        if state_info:
            cache[url] = {
                "state": state_info["state"],
                "source": state_info.get("source", "unknown"),
                "last_fetched": now.isoformat(),
                # Store updatedAt for activity tracking (Issue #241)
                "updatedAt": state_info.get("updatedAt"),
            }
            result["state"] = state_info["state"]
            result["source"] = state_info.get("source", "unknown")
            result["updatedAt"] = state_info.get("updatedAt") or ""
            result["status"] = "fetched"
            fetched_count += 1
        else:
            result["status"] = "error"
            result["error"] = "Could not fetch state"
            error_count += 1

        results.append(result)

    # Add cached results
    for url in sorted(fresh_urls):
        cached = cache.get(url, {})
        results.append(
            {
                "url": url,
                "state": cached.get("state"),
                "source": cached.get("source"),
                "status": "cached",
                "cached_at": cached.get("last_fetched"),
            }
        )

    # Save cache (even if only errors, to track last attempt)
    save_cache(cache_path, cache)

    # Output
    if output_json:
        output = {
            "fetched": fetched_count,
            "cached": len(fresh_urls),
            "errors": error_count,
            "total": len(urls_to_fetch),
            "cache_path": str(cache_path),
            "results": results,
        }
        print(json.dumps(output, indent=2))
        return

    # Rich table output
    table = Table(title=f"[bold]Issue State Cache[/] ({len(urls_to_fetch)} URLs)")
    table.add_column("URL", style="cyan", max_width=50)
    table.add_column("State", style="green")
    table.add_column("Source", style="blue")
    table.add_column("Status", style="yellow")

    for r in results:
        url_display = r["url"]
        if len(url_display) > 50:
            url_display = "..." + url_display[-47:]

        state = r.get("state", "N/A")
        state_style = "green" if state == "OPEN" else "dim" if state == "CLOSED" else ""
        source = r.get("source", "")

        if r["status"] == "fetched":
            status = "[green]✓ Fetched[/]"
        elif r["status"] == "cached":
            status = "[dim]⏸ Cached[/]"
        else:
            status = f"[red]✗ {r.get('error', 'Error')}[/]"

        table.add_row(
            url_display,
            f"[{state_style}]{state}[/]" if state_style else state,
            source,
            status,
        )

    console.print(table)

    # Summary
    console.print(
        f"\n[bold]Summary:[/] {fetched_count} fetched, {len(fresh_urls)} cached, {error_count} errors"
    )
    if fetched_count > 0:
        console.print(f"[dim]Cache saved to: {cache_path}[/]")


# =============================================================================
# Import Command - Import tasks from GitHub/Linear
# =============================================================================


@cli.command("import")
@click.option(
    "--source",
    type=click.Choice(["github", "linear"]),
    required=True,
    help="Source to import from (github or linear)",
)
@click.option(
    "--repo",
    help="GitHub repository in owner/repo format (required for github source)",
)
@click.option(
    "--team",
    help="Linear team key (required for linear source)",
)
@click.option(
    "--state",
    type=click.Choice(["open", "closed", "all"]),
    default="open",
    help="Filter by issue state",
)
@click.option(
    "--label",
    multiple=True,
    help="Filter by label (GitHub only, can be used multiple times)",
)
@click.option(
    "--assignee",
    help="Filter by assignee (GitHub only, username or 'me')",
)
@click.option(
    "--limit",
    default=20,
    type=click.IntRange(min=1, max=100),
    help="Maximum number of issues to import (1-100)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be imported without creating files",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output as JSON for machine consumption",
)
def import_issues(
    source: str,
    repo: Optional[str],
    team: Optional[str],
    state: str,
    label: tuple,
    assignee: Optional[str],
    limit: int,
    dry_run: bool,
    output_json: bool,
):
    """Import issues from GitHub or Linear as placeholder tasks.

    Creates minimal task files with tracking frontmatter linking back
    to the source. Existing tasks with matching tracking URLs are skipped
    to avoid duplicates.

    Examples:
        # Import open issues from a GitHub repo
        gptodo import --source github --repo gptme/gptme --state open

        # Import issues with specific labels
        gptodo import --source github --repo gptme/gptme --label bug

        # Import from Linear (requires LINEAR_API_KEY)
        gptodo import --source linear --team ENG
    """
    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Validate source-specific options
    if source == "github" and not repo:
        console.print("[red]Error: --repo is required for github source[/]")
        sys.exit(1)
    if source == "linear" and not team:
        console.print("[red]Error: --team is required for linear source[/]")
        sys.exit(1)

    # Warn about ignored options
    if source == "linear" and (label or assignee):
        console.print("[yellow]Warning: --label and --assignee are not supported for Linear[/]")

    # Ensure tasks directory exists
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Load existing tasks to check for duplicates
    existing_tasks = load_tasks(tasks_dir)
    existing_tracking: Set[str] = set()
    for task in existing_tasks:
        tracking = task.metadata.get("tracking")
        if tracking:
            if isinstance(tracking, list):
                existing_tracking.update(tracking)
            else:
                existing_tracking.add(tracking)

    # Fetch issues based on source
    if source == "github":
        assert repo is not None  # Validated above
        issues = fetch_github_issues(repo, state, list(label), assignee, limit)
    else:  # linear
        assert team is not None  # Validated above
        issues = fetch_linear_issues(team, state, limit)

    if not issues:
        if output_json:
            print(json.dumps({"imported": [], "count": 0, "message": "No issues found"}))
            return
        console.print("[yellow]No issues found matching criteria[/]")
        return

    # Process issues
    imported = []
    skipped = []
    for issue in issues:
        tracking_ref = issue["tracking_ref"]

        # Check for duplicates
        if tracking_ref in existing_tracking:
            skipped.append(
                {
                    "title": issue["title"],
                    "tracking_ref": tracking_ref,
                    "reason": "Already exists in tasks",
                }
            )
            continue

        # Generate task filename
        task_filename = generate_task_filename(issue["title"], issue["number"], source)
        task_path = tasks_dir / task_filename

        # Skip if file already exists
        if task_path.exists():
            skipped.append(
                {
                    "title": issue["title"],
                    "tracking_ref": tracking_ref,
                    "reason": f"File {task_filename} already exists",
                }
            )
            continue

        # Map priority from labels
        priority = map_priority_from_labels(issue.get("labels", []))

        # Generate task content
        task_content = generate_task_content(issue, source, priority)

        if dry_run:
            imported.append(
                {
                    "title": issue["title"],
                    "tracking_ref": tracking_ref,
                    "filename": task_filename,
                    "dry_run": True,
                }
            )
        else:
            # Create the task file
            try:
                task_path.write_text(task_content, encoding="utf-8")
                imported.append(
                    {
                        "title": issue["title"],
                        "tracking_ref": tracking_ref,
                        "filename": task_filename,
                        "created": True,
                    }
                )
            except Exception as e:
                skipped.append(
                    {
                        "title": issue["title"],
                        "tracking_ref": tracking_ref,
                        "reason": f"Failed to create file: {e}",
                    }
                )

    # Output results
    if output_json:
        result = {
            "imported": imported,
            "skipped": skipped,
            "count": len(imported),
            "skipped_count": len(skipped),
        }
        if dry_run:
            result["dry_run"] = True
        print(json.dumps(result, indent=2))
        return

    # Rich output
    if dry_run:
        console.print("[bold yellow]DRY RUN[/] - No files created\n")

    if imported:
        table = Table(
            title=f"[bold]{'Would Import' if dry_run else 'Imported'} ({len(imported)} issues)[/]"
        )
        table.add_column("Title", style="cyan", max_width=50)
        table.add_column("Tracking", style="blue")
        table.add_column("Filename", style="green")

        for item in imported:
            table.add_row(
                item["title"][:50],
                item["tracking_ref"],
                item["filename"],
            )
        console.print(table)

    if skipped:
        console.print(f"\n[yellow]Skipped {len(skipped)} issues:[/]")
        for item in skipped:
            console.print(f"  - {item['title'][:40]}: {item['reason']}")

    if not dry_run and imported:
        console.print(f"\n[green]✓ Created {len(imported)} task files in {tasks_dir}[/]")
        console.print("[dim]Run 'gptodo sync' to keep states synchronized[/]")


# =============================================================================
# Lock Commands (Phase 3 - Issue #240)
# =============================================================================


@cli.command("lock")
@click.argument("task_id")
@click.option(
    "--worker",
    "-w",
    default=None,
    help="Worker identifier (default: auto-generated from hostname/pid)",
)
@click.option(
    "--timeout",
    "-t",
    default=DEFAULT_LOCK_TIMEOUT_HOURS,
    type=float,
    help=f"Lock timeout in hours (default: {DEFAULT_LOCK_TIMEOUT_HOURS})",
)
@click.option("--force", "-f", is_flag=True, help="Force acquire even if locked by another")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def lock_task(task_id: str, worker: Optional[str], timeout: float, force: bool, output_json: bool):
    """Acquire a lock on a task.

    Prevents multiple agents/processes from working on the same task.
    Locks expire after timeout (default 4 hours).

    Examples:
        gptodo lock my-task
        gptodo lock my-task --worker bob-session-123
        gptodo lock my-task --timeout 2.0
        gptodo lock my-task --force  # Steal lock from another worker
    """
    import socket

    # Generate default worker ID if not provided
    if worker is None:
        worker = f"{socket.gethostname()}-{os.getpid()}"

    repo_root = Path(os.environ.get("TASKS_REPO_ROOT", "."))
    tasks_dir = repo_root / "tasks"

    # First verify the task exists
    all_tasks = load_tasks(tasks_dir)
    tasks = resolve_tasks([task_id], all_tasks, tasks_dir)
    if not tasks:
        if output_json:
            print(json.dumps({"success": False, "error": f"Task not found: {task_id}"}))
        else:
            console = Console()
            console.print(f"[red]Error: Task not found: {task_id}[/]")
        sys.exit(1)

    task = tasks[0]
    actual_task_id = task.id

    # Attempt to acquire lock
    success, existing = acquire_lock(actual_task_id, worker, timeout, repo_root, force)

    if output_json:
        result = {
            "success": success,
            "task_id": actual_task_id,
            "worker": worker,
            "timeout_hours": timeout,
        }
        if existing:
            result["previous_lock"] = {
                "worker": existing.worker,
                "started": existing.started,
                "expired": existing.is_expired(),
            }
        print(json.dumps(result, indent=2))
    else:
        console = Console()
        if success:
            if existing:
                if force:
                    console.print(f"[yellow]⚠ Stole lock from {existing.worker}[/]")
                else:
                    console.print(f"[dim]Previous lock by {existing.worker} had expired[/]")
            console.print(f"[green]✓ Locked task: {actual_task_id}[/]")
            console.print(f"[dim]  Worker: {worker}[/]")
            console.print(f"[dim]  Timeout: {timeout} hours[/]")
        else:
            console.print(f"[red]✗ Failed to lock task: {actual_task_id}[/]")
            if existing:
                age = existing.age_hours()
                console.print(f"[yellow]  Locked by: {existing.worker}[/]")
                console.print(f"[yellow]  Since: {existing.started} ({age:.1f}h ago)[/]")
                console.print("[dim]  Use --force to steal the lock[/]")
            sys.exit(1)


@cli.command("unlock")
@click.argument("task_id")
@click.option(
    "--worker",
    "-w",
    default=None,
    help="Worker identifier (must match lock owner unless --force)",
)
@click.option("--force", "-f", is_flag=True, help="Force release even if not owner")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def unlock_task(task_id: str, worker: Optional[str], force: bool, output_json: bool):
    """Release a lock on a task.

    By default, only the lock owner can release. Use --force to override.

    Examples:
        gptodo unlock my-task
        gptodo unlock my-task --worker bob-session-123
        gptodo unlock my-task --force
    """
    import socket

    # Generate default worker ID if not provided
    if worker is None:
        worker = f"{socket.gethostname()}-{os.getpid()}"

    repo_root = Path(os.environ.get("TASKS_REPO_ROOT", "."))
    tasks_dir = repo_root / "tasks"

    # Resolve task ID
    all_tasks = load_tasks(tasks_dir)
    tasks = resolve_tasks([task_id], all_tasks, tasks_dir)
    actual_task_id = tasks[0].id if tasks else task_id

    success, message = release_lock(actual_task_id, worker, repo_root, force)

    if output_json:
        result = {"success": success, "task_id": actual_task_id}
        if message:
            result["message"] = message
        print(json.dumps(result, indent=2))
    else:
        console = Console()
        if success:
            console.print(f"[green]✓ Unlocked task: {actual_task_id}[/]")
            if message:
                console.print(f"[dim]  {message}[/]")
        else:
            console.print(f"[red]✗ Failed to unlock task: {actual_task_id}[/]")
            if message:
                console.print(f"[yellow]  {message}[/]")
            sys.exit(1)


@cli.command("locks")
@click.option("--cleanup", is_flag=True, help="Remove expired locks")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
def list_all_locks(cleanup: bool, output_json: bool):
    """List all current task locks.

    Shows which tasks are currently locked and by whom.
    Use --cleanup to remove expired locks.

    Examples:
        gptodo locks
        gptodo locks --cleanup
        gptodo locks --json
    """
    repo_root = Path(os.environ.get("TASKS_REPO_ROOT", "."))

    if cleanup:
        removed = cleanup_expired_locks(repo_root)
        if output_json:
            print(
                json.dumps(
                    {
                        "removed": [
                            {"task_id": lock.task_id, "worker": lock.worker} for lock in removed
                        ],
                        "count": len(removed),
                    },
                    indent=2,
                )
            )
        else:
            console = Console()
            if removed:
                console.print(f"[green]✓ Removed {len(removed)} expired lock(s)[/]")
                for lock in removed:
                    console.print(f"[dim]  - {lock.task_id} (was: {lock.worker})[/]")
            else:
                console.print("[dim]No expired locks to remove[/]")
        return

    locks = list_locks(repo_root)

    if output_json:
        print(
            json.dumps(
                {
                    "locks": [
                        {
                            "task_id": lck.task_id,
                            "worker": lck.worker,
                            "started": lck.started,
                            "timeout_hours": lck.timeout_hours,
                            "age_hours": round(lck.age_hours(), 2),
                            "expired": lck.is_expired(),
                        }
                        for lck in locks
                    ],
                    "count": len(locks),
                },
                indent=2,
            )
        )
    else:
        console = Console()
        if not locks:
            console.print("[dim]No active locks[/]")
            return

        table = Table(title="[bold]Task Locks[/]")
        table.add_column("Task", style="cyan")
        table.add_column("Worker", style="green")
        table.add_column("Age", style="yellow")
        table.add_column("Status", style="blue")

        for lock in sorted(locks, key=lambda lck: lck.started, reverse=True):
            age = lock.age_hours()
            status = "[red]EXPIRED[/]" if lock.is_expired() else "[green]ACTIVE[/]"
            table.add_row(
                lock.task_id,
                lock.worker,
                f"{age:.1f}h",
                status,
            )

        console.print(table)
        console.print(f"\n[dim]Total: {len(locks)} lock(s)[/]")


# =============================================================================
# Add Command
# =============================================================================


@cli.command("add")
@click.argument("title")
@click.option(
    "--priority",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    help="Task priority",
)
@click.option(
    "--tags",
    help="Comma-separated tags",
)
@click.option(
    "--assigned-to",
    default="bob",
    help="Who the task is assigned to",
)
@click.option(
    "--state",
    type=click.Choice(["new", "active", "paused", "done", "cancelled", "someday"]),
    default="new",
    help="Initial task state",
)
@click.option(
    "--type",
    "task_type",
    type=click.Choice(["action", "project"]),
    default="action",
    help="Task type (action=single-step, project=multi-step)",
)
def add(
    title: str,
    priority: str,
    tags: Optional[str],
    assigned_to: str,
    state: str,
    task_type: str,
):
    """Create a new task from title and optional stdin body.

    The task filename is generated from the title by converting to lowercase
    and replacing non-alphanumeric characters with hyphens.

    If stdin is provided (piped), it becomes the task body.

    Examples:
        # Simple task
        gptodo add "Fix the login bug"

        # With options
        gptodo add --priority high --tags infra,context "Improve context loading"

        # With body from stdin
        echo "Detailed description here" | gptodo add "Task with body"

        # Multi-line body
        gptodo add "Complex task" << 'EOF'
        ## Subtasks
        - [ ] First step
        - [ ] Second step
        EOF
    """
    import re

    console = Console()
    repo_root = find_repo_root(Path.cwd())
    tasks_dir = repo_root / "tasks"

    # Ensure tasks directory exists
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Generate slug from title
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())
    slug = slug.strip("-")[:50].rstrip("-")
    filename = f"{slug}.md"
    filepath = tasks_dir / filename

    # Check for existing file with same name
    if filepath.exists():
        # Add timestamp suffix to make unique
        timestamp = datetime.now().strftime("%H%M%S")
        filename = f"{slug}-{timestamp}.md"
        filepath = tasks_dir / filename

    # Build frontmatter
    now = datetime.now(timezone.utc)
    frontmatter_data: dict[str, str | list[str]] = {
        "state": state,
        "created": now.isoformat(),
        "priority": priority,
        "task_type": task_type,
        "assigned_to": assigned_to,
    }

    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            frontmatter_data["tags"] = tag_list

    # Check for stdin input (body)
    body = ""
    # Check if stdin has data (non-blocking check)
    if not sys.stdin.isatty():
        # stdin is piped, read it
        body = sys.stdin.read().strip()

    # Build file content
    lines = ["---"]
    lines.append(f"state: {frontmatter_data['state']}")
    lines.append(f"created: {frontmatter_data['created']}")
    lines.append(f"priority: {frontmatter_data['priority']}")
    lines.append(f"task_type: {frontmatter_data['task_type']}")
    lines.append(f"assigned_to: {frontmatter_data['assigned_to']}")
    if "tags" in frontmatter_data:
        lines.append(f"tags: {json.dumps(frontmatter_data['tags'])}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")

    if body:
        lines.append(body)
        lines.append("")

    content = "\n".join(lines)

    # Write file
    filepath.write_text(content)

    console.print(f"[green]✓ Created task:[/] {filepath}")


if __name__ == "__main__":
    cli()
