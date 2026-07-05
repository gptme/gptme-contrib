"""
Claude Code plugin for gptme - spawn Claude Code subagents for coding tasks.

Provides functions to leverage Claude Code (claude CLI) as subagents for:
- analyze: Code reviews, security audits, test coverage analysis
- ask: Answer questions about codebases
- fix: Fix lint errors, build issues, type errors
- implement: Implement features (optionally in isolated worktrees)
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from gptme.tools.base import ToolSpec

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeResult:
    """Result from Claude Code execution."""

    prompt: str
    output: str
    exit_code: int
    duration_seconds: float
    task_type: str
    session_id: str | None = None  # For background tasks


def _check_claude_available() -> bool:
    """Check if claude CLI is available."""
    return shutil.which("claude") is not None


def _run_claude(
    prompt: str,
    work_dir: Path,
    timeout: int,
    output_format: str = "text",
    task_type: str = "general",
) -> ClaudeCodeResult:
    """Run Claude Code synchronously and return results."""
    if not _check_claude_available():
        return ClaudeCodeResult(
            prompt=prompt,
            output="Error: Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
            exit_code=1,
            duration_seconds=0,
            task_type=task_type,
        )

    start_time = time.time()

    cmd = ["claude", "-p", prompt]
    if output_format == "json":
        cmd.extend(["--output-format", "json"])

    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.time() - start_time

        output = result.stdout
        if result.stderr:
            output += f"\n\nSTDERR:\n{result.stderr}"

        return ClaudeCodeResult(
            prompt=prompt,
            output=output,
            exit_code=result.returncode,
            duration_seconds=duration,
            task_type=task_type,
        )
    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return ClaudeCodeResult(
            prompt=prompt,
            output=f"Error: Task timed out after {timeout} seconds",
            exit_code=-1,
            duration_seconds=duration,
            task_type=task_type,
        )
    except Exception as e:
        duration = time.time() - start_time
        return ClaudeCodeResult(
            prompt=prompt,
            output=f"Error: {e}",
            exit_code=-1,
            duration_seconds=duration,
            task_type=task_type,
        )


def _run_background(
    prompt: str,
    work_dir: Path,
    timeout: int,
    task_type: str,
) -> str:
    """Run Claude Code in background tmux session."""
    session_id = f"claude_code_{uuid.uuid4().hex[:8]}"

    # Create tmux session with the command
    cmd = f"cd {shlex.quote(str(work_dir))} && timeout {timeout} claude -p {shlex.quote(prompt)}"

    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_id, cmd],
            check=True,
        )
        return (
            f"Background {task_type} task started in session: {session_id}\n\n"
            f"Monitor with: check_session('{session_id}')\n"
            f"Kill with: kill_session('{session_id}')"
        )
    except subprocess.CalledProcessError as e:
        return f"Error starting background session: {e}"


def analyze(
    prompt: str,
    workspace: str | None = None,
    timeout: int = 600,
    background: bool = False,
) -> ClaudeCodeResult | str:
    """
    Run a code analysis task with Claude Code.

    Use for: code reviews, security audits, test coverage analysis,
    architecture documentation, dependency analysis.

    Args:
        prompt: Analysis task description
        workspace: Directory to analyze (defaults to current directory)
        timeout: Maximum time in seconds (default: 10 minutes)
        background: If True, run in tmux and return session ID

    Returns:
        ClaudeCodeResult with analysis output, or session ID if background=True

    Examples:
        # Security audit
        analyze("Review for security vulnerabilities. List by severity.")

        # Code review
        analyze("Review last 3 commits for code quality issues.")

        # Test coverage
        analyze("Identify critical untested code paths.")
    """
    work_dir = Path(workspace) if workspace else Path.cwd()

    # Warn about long sync calls
    if not background and timeout > 240:
        if timeout >= 300:
            raise ValueError(
                f"Sync call with timeout={timeout}s would block >=5 minutes, "
                f"destroying prompt cache. Use background=True for long tasks."
            )
        logger.warning(
            f"Sync call with timeout={timeout}s may block too long. "
            f"Consider background=True."
        )

    if background:
        return _run_background(prompt, work_dir, timeout, "analysis")
    return _run_claude(prompt, work_dir, timeout, task_type="analysis")


def ask(
    question: str,
    workspace: str | None = None,
    timeout: int = 300,
    background: bool = False,
) -> ClaudeCodeResult | str:
    """
    Ask Claude Code a question about the codebase.

    Use for: understanding code structure, finding implementations,
    explaining complex logic, discovering patterns.

    Args:
        question: Question about the codebase
        workspace: Directory to analyze (defaults to current directory)
        timeout: Maximum time in seconds (default: 5 minutes)
        background: If True, run in tmux and return session ID

    Returns:
        ClaudeCodeResult with answer, or session ID if background=True

    Examples:
        # Understand flow
        ask("How does the authentication flow work?")

        # Find code
        ask("Where is the database connection pool configured?")

        # Explain logic
        ask("What does the retry logic in api_client.py do?")
    """
    work_dir = Path(workspace) if workspace else Path.cwd()

    prompt = f"""Answer this question about the codebase:

{question}

Provide a clear, concise answer with specific file and line references where relevant.
"""

    if background:
        return _run_background(prompt, work_dir, timeout, "question")
    return _run_claude(prompt, work_dir, timeout, task_type="question")


def fix(
    issue: str,
    workspace: str | None = None,
    timeout: int = 600,
    background: bool = False,
    auto_commit: bool = False,
) -> ClaudeCodeResult | str:
    """
    Fix code issues with Claude Code.

    Use for: lint errors, type errors, build failures, test failures,
    deprecation warnings.

    Args:
        issue: Description of the issue to fix
        workspace: Directory to work in (defaults to current directory)
        timeout: Maximum time in seconds (default: 10 minutes)
        background: If True, run in tmux and return session ID
        auto_commit: If True, allow Claude to commit fixes (default: False)

    Returns:
        ClaudeCodeResult with fix details, or session ID if background=True

    Examples:
        # Fix lint errors
        fix("Fix all mypy type errors in src/")

        # Fix build
        fix("The build is failing with 'ModuleNotFoundError', diagnose and fix.")

        # Fix tests
        fix("Fix the failing tests in test_api.py")
    """
    work_dir = Path(workspace) if workspace else Path.cwd()

    commit_instruction = ""
    if auto_commit:
        commit_instruction = (
            "\n\nAfter fixing, commit the changes with a descriptive message."
        )
    else:
        commit_instruction = "\n\nDo NOT commit the changes. Just make the fixes and show what was changed."

    prompt = f"""Fix the following issue in this codebase:

{issue}

Steps:
1. Diagnose the root cause
2. Implement the minimal fix
3. Verify the fix resolves the issue
{commit_instruction}

Show the changes made and explain the fix.
"""

    if background:
        return _run_background(prompt, work_dir, timeout, "fix")
    return _run_claude(prompt, work_dir, timeout, task_type="fix")


def implement(
    feature: str,
    workspace: str | None = None,
    timeout: int = 900,
    background: bool = False,
    use_worktree: bool = False,
    branch_name: str | None = None,
) -> ClaudeCodeResult | str:
    """
    Implement a feature with Claude Code.

    Use for: adding new features, refactoring code, implementing
    enhancements, adding tests.

    Args:
        feature: Description of the feature to implement
        workspace: Directory to work in (defaults to current directory)
        timeout: Maximum time in seconds (default: 15 minutes)
        background: If True, run in tmux and return session ID
        use_worktree: If True, work in a git worktree for isolation
        branch_name: Branch name for worktree (auto-generated if not provided)

    Returns:
        ClaudeCodeResult with implementation details, or session ID if background=True

    Examples:
        # Simple feature
        implement("Add a --verbose flag to the CLI")

        # Complex feature with isolation
        implement("Implement rate limiting for API endpoints", use_worktree=True)

        # Refactoring
        implement("Refactor the database module to use connection pooling")
    """
    work_dir = Path(workspace) if workspace else Path.cwd()

    worktree_instructions = ""
    if use_worktree:
        branch = branch_name or f"feature/{uuid.uuid4().hex[:8]}"
        worktree_instructions = f"""
IMPORTANT: Create a git worktree first for isolation:
1. git worktree add ../worktree-{branch} -b {branch}
2. cd ../worktree-{branch}
3. Make all changes there
4. When done, the changes will be on branch '{branch}'
"""

    prompt = f"""Implement the following feature:

{feature}
{worktree_instructions}
Steps:
1. Understand the existing codebase structure
2. Plan the implementation approach
3. Implement the feature with clean, tested code
4. Add appropriate tests
5. Update documentation if needed

Show the implementation and explain the key design decisions.
"""

    if background:
        return _run_background(prompt, work_dir, timeout, "implement")
    return _run_claude(prompt, work_dir, timeout, task_type="implement")


def check_session(session_id: str) -> str:
    """
    Check status and output of a background Claude Code session.

    Args:
        session_id: The session ID returned by background tasks

    Returns:
        Current output from the session, or status if complete
    """
    try:
        # Check if session exists
        has_result = subprocess.run(
            ["tmux", "has-session", "-t", session_id],
            capture_output=True,
        )

        if has_result.returncode != 0:
            return f"Session {session_id} not found (may have completed)"

        # Capture current output
        capture_result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_id],
            capture_output=True,
            text=True,
        )
        return capture_result.stdout
    except Exception as e:
        return f"Error checking session: {e}"


def kill_session(session_id: str) -> str:
    """
    Kill a background Claude Code session.

    Args:
        session_id: The session ID to kill

    Returns:
        Status message
    """
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", session_id],
            check=True,
        )
        return f"Session {session_id} killed"
    except subprocess.CalledProcessError:
        return f"Session {session_id} not found"


# Tool specification for gptme
tool = ToolSpec(
    name="claude_code",
    desc="Spawn Claude Code subagents for coding tasks (analyze, ask, fix, implement)",
    instructions="""
Claude Code plugin for spawning focused subagents.

**Available Functions:**

1. **analyze(prompt)** - Code reviews, security audits, test coverage
2. **ask(question)** - Answer questions about the codebase
3. **fix(issue)** - Fix lint errors, build issues, type errors
4. **implement(feature)** - Implement features (optionally in worktrees)

**When to use Claude Code vs gptme tools:**
- **Claude Code**: Single-purpose tasks, fresh context needed, parallel work
- **gptme tools**: Multi-step workflows, file modifications with review, interactive debugging

**Cost efficiency**: Claude Code uses $200/mo subscription, cost-effective for
parallel tasks vs API calls.

**Background tasks (for >4 min tasks):**
```python
result = analyze("...", background=True, timeout=1800)
check_session("claude_code_abc123")  # Check progress
kill_session("claude_code_abc123")   # Cancel if needed
```

**Examples:**

```python
# Security audit
analyze("Review for security vulnerabilities, list by severity.")

# Code understanding
ask("How does the authentication flow work in this codebase?")

# Fix issues
fix("Fix all mypy type errors in src/")

# Implement feature
implement("Add a --dry-run flag to the deploy command")

# Complex feature with isolation
implement("Implement caching layer", use_worktree=True)
```
    """,
    examples="""
### Code Analysis

> User: Check this code for security issues
> Assistant: I'll run a security analysis.
```ipython
analyze("Review for security vulnerabilities. Focus on: injection, auth, data exposure. List by severity.")
```

### Code Questions

> User: How does auth work here?
> Assistant: Let me ask Claude Code about the auth flow.
```ipython
ask("How does the authentication flow work? Include file references.")
```

### Fixing Issues

> User: Fix the type errors
> Assistant: I'll have Claude Code fix the type errors.
```ipython
fix("Fix all mypy type errors in src/")
```

### Implementing Features

> User: Add a verbose flag to the CLI
> Assistant: I'll implement that feature.
```ipython
implement("Add a --verbose flag that increases logging output.")
```

### Background Tasks

> User: Do a comprehensive audit
> Assistant: I'll run this in the background since it may take a while.
```ipython
analyze("Full security audit covering OWASP Top 10", background=True, timeout=1800)
```
> System: Background analysis task started in session: claude_code_a1b2c3d4
    """,
    functions=[analyze, ask, fix, implement, check_session, kill_session],
)
