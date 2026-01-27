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


def find_dependent_tasks(
    completed_task_id: str,
    all_tasks: List[TaskInfo],
) -> List[TaskInfo]:
    """Find all tasks that depend on the completed task.

    A task depends on the completed task if:
    - `requires` contains the completed task ID
    - `depends` contains the completed task ID (deprecated alias)
    - `waiting_for` contains the completed task ID

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

        # Check waiting_for field
        waiting_for = task.metadata.get("waiting_for", "")
        if isinstance(waiting_for, str) and completed_task_id in waiting_for:
            dependent_tasks.append(task)
            continue

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
            if isinstance(waiting_for, str) and completed_id in waiting_for:
                # Only clear if this was the only thing being waited on
                # If multiple things, just note that one resolved
                post.metadata.pop("waiting_for", None)
                post.metadata.pop("waiting_since", None)
                changes_made.append("cleared waiting_for")

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
