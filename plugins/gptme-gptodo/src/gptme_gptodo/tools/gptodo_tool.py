"""
gptodo delegation tool for gptme.

Provides Python functions for the coordinator agent to delegate work
to subagents without needing shell access. Wraps gptodo CLI commands.

This enables the "autonomous-team" pattern where the top-level agent
uses only delegation tools (gptodo + save) and cannot directly execute
shell commands or modify code.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from gptme.tools.base import ToolSpec

logger = logging.getLogger(__name__)


def _find_gptme_contrib_root() -> Path | None:
    """Find gptme-contrib root by walking up from this file."""
    for parent in Path(__file__).resolve().parents:
        if parent.name == "gptme-contrib":
            return parent
    return None


def _check_gptodo_available() -> bool:
    """Check if gptodo CLI is available."""
    if shutil.which("gptodo") is not None:
        return True

    contrib_root = _find_gptme_contrib_root()
    if contrib_root is None:
        return False

    gptodo_pkg = contrib_root / "packages" / "gptodo"
    return gptodo_pkg.exists() and shutil.which("uv") is not None


def _run_gptodo(*args: str, timeout: int = 30) -> str:
    """Run a gptodo CLI command and return output."""
    cmd = None
    cwd = None

    if shutil.which("gptodo") is not None:
        cmd = ["gptodo", *args]
    else:
        contrib_root = _find_gptme_contrib_root()
        gptodo_pkg = (contrib_root / "packages" / "gptodo") if contrib_root else None
        if gptodo_pkg and gptodo_pkg.exists() and shutil.which("uv") is not None:
            cmd = ["uv", "run", "python3", "-m", "gptodo", *args]
            cwd = str(gptodo_pkg)

    if cmd is None:
        return (
            "Error: gptodo CLI not found. Install with: uv pip install gptodo\n"
            "Or ensure gptme-contrib is present with packages/gptodo and uv is available."
        )

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\nError: {result.stderr.strip()}"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: gptodo command timed out after {timeout}s"
    except Exception as e:
        return f"Error running gptodo: {e}"


def delegate(
    prompt: str,
    task_id: str | None = None,
    backend: str = "gptme",
    agent_type: str = "execute",
    timeout: int = 600,
    background: bool = True,
) -> str:
    """Delegate a task to a subagent.

    Spawns a new agent to work on the given task. The subagent runs
    independently with its own context window.

    Args:
        prompt: Clear description of what the subagent should do.
            Include specific files, goals, and success criteria.
        task_id: Optional task ID to associate with the agent.
            If None, a task is created from the prompt.
        backend: Agent backend to use ('gptme' or 'claude').
        agent_type: Type of agent ('execute', 'plan', 'explore', 'general').
        timeout: Maximum time in seconds for the agent (default: 600).
        background: If True, spawn in background (default). If False, run
            in foreground and wait for completion.

    Returns:
        Status message with session ID for tracking.

    Example:
        >>> delegate("Fix the failing test in tests/test_auth.py by updating the mock")
        'Spawned agent abc123 for task fix-failing-test (background)'
        >>> delegate("Analyze the codebase structure", agent_type="explore")
        'Spawned agent def456 for exploration task (background)'
    """
    args = []

    if background:
        args.extend(["spawn"])
    else:
        args.extend(["run"])

    if task_id:
        args.append(task_id)
        args.extend(["--prompt", prompt])
    else:
        # Create inline task from prompt
        args.extend(["--inline", prompt])

    args.extend(["--backend", backend])
    args.extend(["--type", agent_type])
    args.extend(["--timeout", str(timeout)])

    return _run_gptodo(*args, timeout=timeout + 30)


def check_agent(session_id: str) -> str:
    """Check the status of a delegated agent.

    Args:
        session_id: The session ID returned by delegate().

    Returns:
        Status information including state, output, and any errors.

    Example:
        >>> check_agent("agent_abc123")
        'Status: completed\\nOutput: Fixed test by updating mock...'
    """
    return _run_gptodo("status", session_id)


def list_agents() -> str:
    """List all active and recent agent sessions.

    Returns:
        Table of agent sessions with status, task, and timing info.

    Example:
        >>> list_agents()
        'ID          Status     Task                Duration\\nagent_abc   completed  fix-tests           2m30s\\n...'
    """
    return _run_gptodo("agents", "--json")


def list_tasks(state: str = "active") -> str:
    """List tasks in the workspace.

    Args:
        state: Filter by state ('active', 'new', 'all', 'done').

    Returns:
        List of tasks with their status and priority.

    Example:
        >>> list_tasks()
        'Active tasks:\\n  fix-auth-bug (high) - Fix authentication...'
    """
    args = ["list"]
    if state == "active":
        args.append("--active-only")
    # For other states, just list all and let user filter
    # The CLI doesn't support --state directly
    return _run_gptodo(*args)


def task_status(compact: bool = True) -> str:
    """Get overview of all task statuses.

    Args:
        compact: If True, show compact summary (default).

    Returns:
        Task status overview.

    Example:
        >>> task_status()
        'Tasks: 3 active, 5 done, 2 new'
    """
    args = ["status"]
    if compact:
        args.append("--compact")
    return _run_gptodo(*args)


def add_task(
    title: str,
    description: str = "",
    priority: str = "medium",
    task_type: str = "action",
) -> str:
    """Create a new task.

    Args:
        title: Short title for the task.
        description: Detailed description of what needs to be done.
        priority: Task priority ('low', 'medium', 'high').
        task_type: Task type ('action' for single-step, 'project' for multi-step).

    Returns:
        Confirmation with the created task ID.

    Example:
        >>> add_task("Fix auth bug", "The login endpoint returns 500", priority="high")
        'Created task: fix-auth-bug (high priority)'
    """
    args = ["add", title]
    if description:
        args.extend(["--description", description])
    args.extend(["--priority", priority])
    args.extend(["--type", task_type])
    return _run_gptodo(*args)


instructions = """\
Use these Python functions to delegate work to subagents and manage tasks.

**You are a COORDINATOR agent.** Your role is to:
1. Break down complex work into focused subtasks
2. Delegate each subtask to a specialized subagent
3. Monitor progress and synthesize results
4. Write the session journal with full context

**Available functions:**

```python
# Delegate work to a subagent
delegate(prompt, task_id=None, backend="gptme", agent_type="execute", timeout=600, background=True)

# Check agent status
check_agent(session_id)

# List all agents
list_agents()

# Task management
list_tasks(state="active")
task_status(compact=True)
add_task(title, description="", priority="medium", task_type="action")
```

**Delegation best practices:**
- Write clear, specific prompts with file paths and success criteria
- Use `agent_type="execute"` for code changes, `"explore"` for research
- Use `background=True` (default) for parallel work
- Use `background=False` for sequential tasks where you need the result
- Check agent status before synthesizing results

**Example workflow:**
```python
# 1. Check what needs doing
task_status()

# 2. Delegate focused subtasks
s1 = delegate("Fix the failing test in tests/test_auth.py by updating the mock setup")
s2 = delegate("Add type hints to src/auth/handler.py", agent_type="execute")

# 3. Check progress
check_agent("agent_abc123")
list_agents()
```
"""

examples = """\
### Delegate a task to a subagent

> User: fix the failing tests and update docs
> Assistant: I'll delegate these as separate focused tasks.
```ipython
result = delegate("Fix failing test in tests/test_auth.py - update mock to match new API response format", agent_type="execute")
print(result)
```
> System: Spawned agent agent_a1b2c3 (background, timeout=600s)
> Assistant: Now the documentation update:
```ipython
result = delegate("Update README.md authentication section to reflect new OAuth2 flow", agent_type="execute")
print(result)
```
> System: Spawned agent agent_d4e5f6 (background, timeout=600s)
> Assistant: Both tasks delegated. Let me check their progress:
```ipython
print(list_agents())
```

### Check agent status

> User: how's the work going?
> Assistant: Let me check on the agents:
```ipython
print(check_agent("agent_a1b2c3"))
```
"""


tool = ToolSpec(
    name="gptodo",
    desc="Delegate work to subagents and manage tasks (coordinator mode)",
    instructions=instructions,
    examples=examples,
    functions=[
        delegate,
        check_agent,
        list_agents,
        list_tasks,
        task_status,
        add_task,
    ],
    available=_check_gptodo_available,
)
