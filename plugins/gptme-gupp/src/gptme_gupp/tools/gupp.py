"""
GUPP (Work Persistence) tool for gptme.

Provides functions to track work across session boundaries.
Based on Gas Town's "Gastown Universal Propulsion Principle":
"If there is work on your hook, YOU MUST RUN IT"
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Conditional import for ToolSpec - allows testing without gptme installed
if TYPE_CHECKING:
    from gptme.tools.base import ToolSpec
else:
    try:
        from gptme.tools.base import ToolSpec
    except ImportError:
        ToolSpec = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# Hook schema version
HOOK_VERSION = 1


def _get_hooks_dir() -> Path:
    """Get the hooks directory, creating it if necessary."""
    # Default to state/hooks in current working directory
    hooks_dir = Path.cwd() / "state" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir


def _sanitize_task_id(task_id: str) -> str:
    """Sanitize task_id for safe use in filenames."""
    return task_id.replace("/", "-").replace("\\", "-")


def _hook_path(task_id: str) -> Path:
    """Get path for a hook file."""
    return _get_hooks_dir() / f"{_sanitize_task_id(task_id)}.json"


def _now() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def hook_start(
    task_id: str,
    context_summary: str,
    next_action: str,
    current_step: str = "Starting",
    priority: str = "medium",
) -> str:
    """
    Create a new hook for a task.

    Args:
        task_id: Unique task identifier (e.g., "implement-feature-x")
        context_summary: Brief summary of current context/state
        next_action: Clear description of what to do next
        current_step: Description of current step (default: "Starting")
        priority: Task priority - "low", "medium", or "high"

    Returns:
        Confirmation message with hook details

    Example:
        >>> hook_start("fix-auth-bug", "User login failing", "Debug auth middleware")
        '✅ Hook created: fix-auth-bug\\n   Priority: medium\\n   Next: Debug auth middleware'
    """
    hook = {
        "version": HOOK_VERSION,
        "task_id": task_id,
        "created_at": _now(),
        "updated_at": _now(),
        "current_step": current_step,
        "context_summary": context_summary,
        "next_action": next_action,
        "priority": priority,
        "partial_results": [],
    }

    hook_file = _hook_path(task_id)
    hook_file.write_text(json.dumps(hook, indent=2))
    logger.info(f"Created hook: {task_id}")

    return (
        f"✅ Hook created: {task_id}\n   Priority: {priority}\n   Next: {next_action}"
    )


def hook_update(
    task_id: str,
    current_step: str | None = None,
    context_summary: str | None = None,
    next_action: str | None = None,
    partial_results: list[Any] | None = None,
) -> str:
    """
    Update an existing hook.

    Args:
        task_id: Task identifier to update
        current_step: New current step description
        context_summary: Updated context summary
        next_action: Updated next action
        partial_results: Intermediate results to preserve

    Returns:
        Confirmation message or error if hook not found

    Example:
        >>> hook_update("fix-auth-bug", current_step="Step 2", next_action="Add tests")
        '✅ Hook updated: fix-auth-bug'
    """
    hook_file = _hook_path(task_id)

    if not hook_file.exists():
        return f"❌ Hook not found: {task_id}"

    hook = json.loads(hook_file.read_text())
    hook["updated_at"] = _now()

    if current_step is not None:
        hook["current_step"] = current_step
    if context_summary is not None:
        hook["context_summary"] = context_summary
    if next_action is not None:
        hook["next_action"] = next_action
    if partial_results is not None:
        existing = hook.get("partial_results", [])
        hook["partial_results"] = existing + partial_results

    hook_file.write_text(json.dumps(hook, indent=2))
    logger.info(f"Updated hook: {task_id}")

    return f"✅ Hook updated: {task_id}"


def hook_complete(task_id: str) -> str:
    """
    Mark a hook as complete and remove it.

    Args:
        task_id: Task identifier to complete

    Returns:
        Confirmation message

    Example:
        >>> hook_complete("fix-auth-bug")
        '✅ Hook completed: fix-auth-bug'
    """
    hook_file = _hook_path(task_id)

    if not hook_file.exists():
        return f"❌ Hook not found: {task_id}"

    hook_file.unlink()
    logger.info(f"Completed hook: {task_id}")

    return f"✅ Hook completed: {task_id}"


def hook_list() -> list[dict[str, Any]]:
    """
    Get all pending hooks.

    Returns:
        List of hook data dictionaries, sorted by priority then update time

    Example:
        >>> hooks = hook_list()
        >>> for h in hooks:
        ...     print(f"{h['task_id']}: {h['next_action']}")
    """
    hooks_dir = _get_hooks_dir()
    hooks = []

    for hook_file in hooks_dir.glob("*.json"):
        try:
            hook = json.loads(hook_file.read_text())
            hooks.append(hook)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Error reading hook file {hook_file}: {e}")

    # Sort by priority (high first) then by updated_at (recent first)
    # Use two-step stable sort: first by time descending, then by priority
    priority_order = {"high": 0, "medium": 1, "low": 2}
    # First sort by updated_at descending (recent first)
    hooks.sort(key=lambda h: h.get("updated_at", ""), reverse=True)
    # Then stable sort by priority (high first) - preserves time order within groups
    hooks.sort(key=lambda h: priority_order.get(h.get("priority", "medium"), 1))

    return hooks


def hook_status(stale_threshold_hours: int = 24) -> str:
    """
    Get formatted status of all pending hooks.

    Args:
        stale_threshold_hours: Hours after which a hook is considered stale

    Returns:
        Formatted summary suitable for display

    Example:
        >>> print(hook_status())
        ## Pending Work Hooks (GUPP)
        ...
    """
    hooks = hook_list()

    if not hooks:
        return "✅ No pending hooks - all work is complete!"

    threshold = datetime.now(timezone.utc) - timedelta(hours=stale_threshold_hours)

    lines = ["## Pending Work Hooks (GUPP)", ""]
    lines.append("**RULE**: If there is work on your hook, YOU MUST RUN IT")
    lines.append("")

    for hook in hooks:
        task_id = hook.get("task_id", "unknown")
        priority = hook.get("priority", "medium")
        next_action = hook.get("next_action", "Continue work")
        context = hook.get("context_summary", "")
        updated_str = hook.get("updated_at", "")

        # Check if stale
        is_stale = False
        if updated_str:
            try:
                updated = datetime.fromisoformat(updated_str)
                is_stale = updated < threshold
            except (ValueError, TypeError):
                pass

        stale_marker = " ⚠️ STALE" if is_stale else ""

        lines.append(f"### Hook: {task_id}{stale_marker}")
        lines.append(f"- **Priority**: {priority}")
        lines.append(f"- **Next Action**: {next_action}")
        if context:
            # Truncate context if needed (UTF-8 safe, only add ellipsis when truncating)
            context_display = f"{context[:200]}..." if len(context) > 200 else context
            lines.append(f"- **Context**: {context_display}")
        lines.append(f"- **Updated**: {updated_str}")

        if is_stale:
            lines.append(
                f"- **⚠️ WARNING**: Hook is stale (>{stale_threshold_hours}h old). "
                "Consider: continue work, update hook, or abandon with reason."
            )

        lines.append("")

    return "\n".join(lines)


def hook_abandon(task_id: str, reason: str) -> str:
    """
    Abandon a hook (moves to archive with reason).

    Args:
        task_id: Task identifier to abandon
        reason: Reason for abandonment

    Returns:
        Confirmation message

    Example:
        >>> hook_abandon("fix-auth-bug", "Issue resolved differently in PR #123")
        '✅ Hook abandoned: fix-auth-bug'
    """
    hook_file = _hook_path(task_id)

    if not hook_file.exists():
        return f"❌ Hook not found: {task_id}"

    hook = json.loads(hook_file.read_text())
    hook["abandoned_at"] = _now()
    hook["abandon_reason"] = reason

    # Move to archive (use sanitized task_id for filename safety)
    archive_dir = _get_hooks_dir() / "archive"
    archive_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive_path = archive_dir / f"{_sanitize_task_id(task_id)}-{timestamp}.json"
    archive_path.write_text(json.dumps(hook, indent=2))

    hook_file.unlink()
    logger.info(f"Abandoned hook: {task_id} - {reason}")

    return f"✅ Hook abandoned: {task_id}\n   Reason: {reason}"


# Tool specification for gptme (only created if gptme is installed)
tool = None
if ToolSpec is not None:
    tool = ToolSpec(
        name="gupp",
        desc="Work persistence across session boundaries using the GUPP pattern",
        instructions="""
Use this tool to track work across session boundaries.

**Core Principle (GUPP)**: "If there is work on your hook, YOU MUST RUN IT"

**Functions available:**

| Function | Purpose |
|----------|---------|
| `hook_start(task_id, context, next_action)` | Create a new work hook |
| `hook_update(task_id, current_step, next_action)` | Update hook progress |
| `hook_complete(task_id)` | Mark work as complete |
| `hook_list()` | Get all pending hooks |
| `hook_status()` | Formatted status summary |
| `hook_abandon(task_id, reason)` | Abandon with reason |

**Workflow:**

1. **At session start**: Call `hook_status()` to check for pending work
2. **Starting work**: Call `hook_start()` to create a hook
3. **During work**: Call `hook_update()` to track progress
4. **On completion**: Call `hook_complete()` to clean up
5. **If abandoning**: Call `hook_abandon()` with reason

**Example:**
```python
# Check for pending work
print(hook_status())

# Start new work
hook_start("fix-auth-bug", "User login failing", "Debug auth middleware")

# Update progress
hook_update("fix-auth-bug", current_step="Step 2", next_action="Add tests")

# Complete when done
hook_complete("fix-auth-bug")
```
        """,
        functions=[
            hook_start,
            hook_update,
            hook_complete,
            hook_list,
            hook_status,
            hook_abandon,
        ],
    )
