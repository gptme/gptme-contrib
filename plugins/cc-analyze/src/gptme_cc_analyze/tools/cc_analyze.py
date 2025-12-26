"""
Claude Code analysis tool for spawning focused analysis subagents.

Runs Claude Code (claude CLI) for analysis tasks that don't need gptme's full toolset.
Useful for security audits, code reviews, test coverage analysis, etc.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from gptme.tools.base import ToolSpec

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """Result from Claude Code analysis."""

    prompt: str
    output: str
    exit_code: int
    duration_seconds: float
    session_id: str | None = None  # For background tasks


def analyze(
    prompt: str,
    workspace: str | None = None,
    timeout: int = 600,
    background: bool = False,
    output_format: str = "text",
) -> AnalysisResult | str:
    """
    Run a Claude Code analysis task.

    Spawns a Claude Code subagent to perform focused analysis work.
    Useful for tasks that don't need gptme's full tool ecosystem.

    Args:
        prompt: Analysis task description/prompt
        workspace: Directory to run in (defaults to current directory)
        timeout: Maximum time in seconds (default: 10 minutes)
        background: If True, run in tmux and return session ID
        output_format: Output format - 'text' or 'json'

    Returns:
        AnalysisResult with output and metadata, or session ID if background=True

    Examples:
        # Quick security scan
        analyze("Review this codebase for security vulnerabilities. List findings by severity.")

        # Code review
        analyze("Review the changes in the last 5 commits for code quality issues.")

        # Test coverage analysis
        analyze("Analyze test coverage and identify critical untested paths.")

        # Architecture review
        analyze("Document the architecture of this project with a focus on data flow.")
    """
    # Verify claude CLI is available
    if not _check_claude_available():
        return AnalysisResult(
            prompt=prompt,
            output="Error: Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
            exit_code=1,
            duration_seconds=0,
        )

    work_dir = Path(workspace) if workspace else Path.cwd()

    if background:
        return _run_background(prompt, work_dir, timeout)
    else:
        return _run_sync(prompt, work_dir, timeout, output_format)


def _check_claude_available() -> bool:
    """Check if claude CLI is available."""
    try:
        result = subprocess.run(
            ["which", "claude"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run_sync(
    prompt: str,
    work_dir: Path,
    timeout: int,
    output_format: str,
) -> AnalysisResult:
    """Run Claude Code synchronously and return results."""
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

        return AnalysisResult(
            prompt=prompt,
            output=output,
            exit_code=result.returncode,
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return AnalysisResult(
            prompt=prompt,
            output=f"Error: Analysis timed out after {timeout} seconds",
            exit_code=-1,
            duration_seconds=duration,
        )
    except Exception as e:
        duration = time.time() - start_time
        return AnalysisResult(
            prompt=prompt,
            output=f"Error: {e}",
            exit_code=-1,
            duration_seconds=duration,
        )


def _run_background(
    prompt: str,
    work_dir: Path,
    timeout: int,
) -> str:
    """Run Claude Code in background tmux session."""
    import uuid

    session_id = f"cc_analyze_{uuid.uuid4().hex[:8]}"

    # Escape prompt for shell
    escaped_prompt = prompt.replace("'", "'\"'\"'")

    # Create tmux session with the command
    cmd = f"cd '{work_dir}' && timeout {timeout} claude -p '{escaped_prompt}'"

    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_id, cmd],
            check=True,
        )
        return f"Background analysis started in session: {session_id}\n\nMonitor with: tmux capture-pane -p -t {session_id}\nKill with: tmux kill-session -t {session_id}"
    except subprocess.CalledProcessError as e:
        return f"Error starting background session: {e}"


def check_session(session_id: str) -> str:
    """
    Check the status and output of a background analysis session.

    Args:
        session_id: The tmux session ID returned by analyze(background=True)

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
    Kill a background analysis session.

    Args:
        session_id: The tmux session ID to kill

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
    name="cc_analyze",
    desc="Spawn Claude Code analysis subagents for focused analysis tasks",
    instructions="""
Use this tool to spawn Claude Code for focused analysis tasks.
Useful for security audits, code reviews, test coverage analysis, etc.

**When to use cc_analyze vs gptme tools:**
- Use cc_analyze: Single-purpose analysis, no file modifications needed
- Use gptme tools: Complex workflows, file modifications, multi-step tasks

**Cost efficiency**: Claude Code uses a $200/mo subscription, making it
cost-effective for parallel analysis tasks vs API calls.

**Examples:**

Quick security scan:
```cc_analyze
analyze("Review this codebase for security vulnerabilities. Focus on: injection attacks, auth issues, data exposure. List findings by severity (Critical/High/Medium/Low) with file locations.")
```

Code review:
```cc_analyze
analyze("Review changes in the last commit for: code quality, potential bugs, performance issues, missing tests. Provide specific line-level feedback.")
```

Test coverage analysis:
```cc_analyze
analyze("Analyze test coverage. Identify: critical untested paths, missing edge cases, integration test gaps. Prioritize by risk.")
```

Architecture documentation:
```cc_analyze
analyze("Document the architecture of this project. Include: component diagram, data flow, key abstractions, external dependencies.")
```

Background analysis (for long tasks):
```cc_analyze
analyze("Comprehensive security audit of entire codebase", background=True, timeout=1800)
```

Check background task:
```cc_analyze
check_session("cc_analyze_abc12345")
```
    """,
    examples="""
### Quick Security Scan

> User: Check this code for security issues
> Assistant: I'll run a focused security analysis with Claude Code.
```cc_analyze
analyze("Review this codebase for security vulnerabilities. List findings by severity with file locations and remediation suggestions.")
```
> System: === Analysis Complete (45.2s) ===
> ## Security Findings
> ### Critical
> - SQL injection in api/users.py:45 - use parameterized queries
> ### High
> - Hardcoded API key in config.py:12 - move to environment variable

### Code Review

> User: Review the last few commits
> Assistant: Let me analyze recent changes for quality issues.
```cc_analyze
analyze("Review the last 3 commits for code quality, potential bugs, and style issues. Provide specific feedback.")
```

### Background Analysis

> User: Do a comprehensive security audit (this might take a while)
> Assistant: I'll start a background analysis since this could take 15+ minutes.
```cc_analyze
analyze("Comprehensive security audit covering OWASP Top 10, dependency vulnerabilities, auth/authz issues, and data handling", background=True, timeout=1800)
```
> System: Background analysis started in session: cc_analyze_a1b2c3d4
>
> Monitor with: tmux capture-pane -p -t cc_analyze_a1b2c3d4

### Check Background Task

> User: How's that security audit going?
> Assistant: Let me check the progress.
```cc_analyze
check_session("cc_analyze_a1b2c3d4")
```
    """,
    functions=[analyze, check_session, kill_session],
    block_types=["cc_analyze"],
)
