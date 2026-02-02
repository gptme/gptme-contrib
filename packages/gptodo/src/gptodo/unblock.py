"""Auto-unblocking functionality for gptodo.

When a task is marked as done, automatically update dependent tasks:
1. Find all tasks with `requires: [<task_id>]` or `waiting_for: <task>`
2. Check if all dependencies are now satisfied
3. Clear `waiting_for` if it was pointing to the completed task
4. Report which tasks were unblocked
"""

from pathlib import Path
from typing import List, Dict, Tuple, Optional
import frontmatter

from gptodo.utils import TaskInfo
from gptodo.waiting import parse_waiting_for, WaitType


def find_dependent_tasks(
    completed_task_id: str,
    all_tasks: List[TaskInfo],
) -> List[TaskInfo]:
    """Find all tasks that depend on the completed task.

    A task depends on the completed task if:
    - `requires` contains the completed task ID
    - `depends` contains the completed task ID (deprecated alias)
    - `waiting_for` contains the completed task ID (string, dict, or list format)

    Args:
        completed_task_id: ID/name of the task that was completed
        all_tasks: List of all tasks

    Returns:
        List of tasks that depend on the completed task
    """
    dependent_tasks = []

    for task in all_tasks:
        # Skip the completed task itself
        if task.id == completed_task_id or task.name == completed_task_id:
            continue

        # Check requires (canonical)
        if completed_task_id in task.requires:
            dependent_tasks.append(task)
            continue

        # Check depends (deprecated alias)
        if completed_task_id in task.depends:
            dependent_tasks.append(task)
            continue

        # Check waiting_for field (supports string, dict, and list formats)
        conditions = parse_waiting_for(task.metadata)
        for condition in conditions:
            # For TASK type, check if ref matches the completed task
            if condition.type == WaitType.TASK and completed_task_id in condition.ref:
                dependent_tasks.append(task)
                break

    return dependent_tasks


def auto_unblock_tasks(
    completed_task_ids: List[str],
    all_tasks: List[TaskInfo],
    tasks_dir: Path,
    issue_cache: Optional[Dict] = None,
) -> List[Tuple[str, str]]:
    """Auto-unblock tasks that were waiting on completed tasks.

    Args:
        completed_task_ids: IDs/names of tasks that were marked done
        all_tasks: List of all tasks
        tasks_dir: Path to the tasks directory
        issue_cache: Optional cache of issue states for URL-based requires

    Returns:
        List of (task_id, action) tuples describing what was unblocked:
        - ("task-name", "cleared waiting_for")
        - ("task-name", "now ready")
    """
    unblocked: List[Tuple[str, str]] = []

    # Build task lookup dict
    tasks_by_id = {t.id: t for t in all_tasks}
    tasks_by_name = {t.name: t for t in all_tasks}
    all_tasks_dict = {**tasks_by_id, **tasks_by_name}

    for completed_id in completed_task_ids:
        # Find tasks that depend on this completed task
        dependent_tasks = find_dependent_tasks(completed_id, all_tasks)

        for task in dependent_tasks:
            # Skip if task is already done/cancelled
            if task.state in ["done", "cancelled"]:
                continue

            changes_made = []

            # We need to reload from disk to get fresh state
            task_path = task.path
            post = frontmatter.load(task_path)

            # Clear waiting_for if it was pointing to the completed task
            waiting_for = post.metadata.get("waiting_for", "")

            # Handle legacy string format
            if isinstance(waiting_for, str) and completed_id in waiting_for:
                # Only clear if this was the only thing being waited on
                # Check for exact match (with optional whitespace)
                waiting_for_stripped = waiting_for.strip()
                if waiting_for_stripped == completed_id:
                    # Exact match - clear both fields
                    post.metadata.pop("waiting_for", None)
                    post.metadata.pop("waiting_since", None)
                    changes_made.append("cleared waiting_for")
                else:
                    # Partial match - task ID is mentioned but there's more text
                    # This could be multiple tasks or descriptive text like "PR #123 review"
                    # Don't clear, but note the dependency was resolved
                    changes_made.append(f"dependency {completed_id} resolved (still waiting)")

            # Handle structured formats (dict or list)
            elif isinstance(waiting_for, (dict, list)):
                conditions = parse_waiting_for(post.metadata)
                # Find TASK conditions that reference the completed task
                remaining_conditions = []
                cleared_any = False
                for condition in conditions:
                    if condition.type == WaitType.TASK and completed_id in condition.ref:
                        cleared_any = True
                    else:
                        remaining_conditions.append(condition)

                if cleared_any:
                    if not remaining_conditions:
                        # All conditions cleared
                        post.metadata.pop("waiting_for", None)
                        post.metadata.pop("waiting_since", None)
                        changes_made.append("cleared waiting_for")
                    else:
                        # Some conditions remain - update to remaining only
                        if len(remaining_conditions) == 1:
                            post.metadata["waiting_for"] = remaining_conditions[0].to_dict()
                        else:
                            post.metadata["waiting_for"] = [
                                c.to_dict() for c in remaining_conditions
                            ]
                        changes_made.append(f"dependency {completed_id} resolved (still waiting)")

            # Check if task is now fully unblocked using the existing task object
            # Update requires from the modified metadata
            task_requires = post.metadata.get("requires", []) or post.metadata.get("depends", [])

            # Check if all dependencies are satisfied
            all_deps_done = True
            for dep_name in task_requires:
                # Skip URL-based dependencies
                if isinstance(dep_name, str) and dep_name.startswith("http"):
                    continue
                dep_task = all_tasks_dict.get(dep_name)
                if dep_task and dep_task.state not in ["done", "cancelled"]:
                    all_deps_done = False
                    break
                elif dep_task is None:
                    # Unknown dependency - assume still blocked
                    all_deps_done = False
                    break

            if all_deps_done and "cleared waiting_for" not in changes_made:
                changes_made.append("now ready")

            # Save changes if any were made
            if changes_made:
                with open(task_path, "w") as f:
                    f.write(frontmatter.dumps(post))
                unblocked.append((task.name, ", ".join(changes_made)))

    return unblocked


def check_fan_in_completion(
    completed_task: TaskInfo,
    all_tasks: list,
    tasks_dir: Path,
) -> tuple[str, str] | None:
    """Check if a completed subtask causes its parent to complete (fan-in).

    When a spawned subtask completes, check if all sibling subtasks are done.
    If so, mark the parent task as done, completing the fan-in pattern.

    Args:
        completed_task: The task that was just completed
        all_tasks: List of all tasks for lookup
        tasks_dir: Path to the tasks directory

    Returns:
        Tuple of (parent_task_id, action) if parent was completed, None otherwise
    """
    # Check if this task was spawned from a parent
    if not completed_task.spawned_from:
        return None

    parent_id = completed_task.spawned_from

    # Build task lookup dict
    tasks_by_id = {t.id: t for t in all_tasks}
    tasks_by_name = {t.name: t for t in all_tasks}
    all_tasks_dict = {**tasks_by_id, **tasks_by_name}

    # Find the parent task
    parent_task = all_tasks_dict.get(parent_id)
    if not parent_task:
        return None

    # Skip if parent is already done/cancelled
    if parent_task.state in ["done", "cancelled"]:
        return None

    # Check if all spawned subtasks are done
    spawned_tasks = parent_task.spawned_tasks
    if not spawned_tasks:
        return None

    remaining = []
    for subtask_id in spawned_tasks:
        subtask = all_tasks_dict.get(subtask_id)
        if subtask and subtask.state not in ["done", "cancelled"]:
            remaining.append(subtask_id)

    if remaining:
        # Not all subtasks are done yet
        return None

    # All subtasks are done! Mark parent as done
    parent_path = parent_task.path
    post = frontmatter.load(parent_path)
    post.metadata["state"] = "done"

    with open(parent_path, "w") as f:
        f.write(frontmatter.dumps(post))

    return (parent_task.id, "all subtasks done (fan-in complete)")


def auto_unblock_with_fan_in(
    completed_task_ids: List[str],
    all_tasks: list,
    tasks_dir: Path,
    issue_cache: Optional[Dict] = None,
) -> List[Tuple[str, str]]:
    """Auto-unblock tasks and handle fan-in completion aggregation.

    This is the main entry point that combines:
    1. Standard auto-unblocking (clearing waiting_for, checking deps)
    2. Fan-in completion (marking parent done when all subtasks complete)

    Args:
        completed_task_ids: IDs/names of tasks that were marked done
        all_tasks: List of all tasks
        tasks_dir: Path to the tasks directory
        issue_cache: Optional cache of issue states for URL-based requires

    Returns:
        List of (task_id, action) tuples describing what was unblocked/completed
    """
    results: List[Tuple[str, str]] = []

    # Build task lookup dict
    tasks_by_id = {t.id: t for t in all_tasks}
    tasks_by_name = {t.name: t for t in all_tasks}
    all_tasks_dict = {**tasks_by_id, **tasks_by_name}

    # Track newly completed parent tasks for recursive unblocking
    newly_completed = list(completed_task_ids)

    # First, check for fan-in completions
    for task_id in completed_task_ids:
        completed_task = all_tasks_dict.get(task_id)
        if completed_task:
            fan_in_result = check_fan_in_completion(completed_task, all_tasks, tasks_dir)
            if fan_in_result:
                results.append(fan_in_result)
                # Add parent to list for recursive unblocking
                newly_completed.append(fan_in_result[0])

    # Then do standard auto-unblocking for all completed tasks (including parents)
    unblock_results = auto_unblock_tasks(newly_completed, all_tasks, tasks_dir, issue_cache)
    results.extend(unblock_results)

    return results
