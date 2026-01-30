"""Sub-agent spawning and management for gptodo.

Enables Claude Code-style sub-agent spawning via gptme subprocesses.
Supports both foreground and background (tmux) execution.

Implements Issue #255: "winning combination" multi-agent collaboration.

Session data stored in state/sessions/ directory (gitignored).
"""

import json
import logging
import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal

logger = logging.getLogger(__name__)

# Directory for session state files
SESSIONS_DIR = "state/sessions"


@dataclass
class AgentSession:
    """Represents a sub-agent session."""

    session_id: str
    task_id: str
    agent_type: Literal["general", "explore", "plan", "execute"]
    backend: Literal["gptme", "claude", "codex"]
    started: str
    status: Literal["running", "completed", "failed", "killed"]
    tmux_session: Optional[str] = None
    output_file: Optional[str] = None
    error: Optional[str] = None
    completed_at: Optional[str] = None


def get_sessions_dir(workspace: Optional[Path] = None) -> Path:
    """Get the sessions directory, creating if needed."""
    if workspace is None:
        workspace = Path.cwd()
    sessions_dir = workspace / SESSIONS_DIR
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


def load_session(session_id: str, workspace: Optional[Path] = None) -> Optional[AgentSession]:
    """Load a session by ID."""
    sessions_dir = get_sessions_dir(workspace)
    session_file = sessions_dir / f"{session_id}.json"

    if not session_file.exists():
        return None

    try:
        data = json.loads(session_file.read_text())
        return AgentSession(**data)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Error loading session {session_id}: {e}")
        return None


def save_session(session: AgentSession, workspace: Optional[Path] = None) -> Path:
    """Save a session to disk."""
    sessions_dir = get_sessions_dir(workspace)
    session_file = sessions_dir / f"{session.session_id}.json"
    session_file.write_text(json.dumps(asdict(session), indent=2))
    return session_file


def list_sessions(
    workspace: Optional[Path] = None, status: Optional[str] = None
) -> list[AgentSession]:
    """List all sessions, optionally filtered by status."""
    sessions_dir = get_sessions_dir(workspace)
    sessions = []

    for session_file in sessions_dir.glob("*.json"):
        session = load_session(session_file.stem, workspace)
        if session:
            if status is None or session.status == status:
                sessions.append(session)

    # Sort by start time, newest first
    sessions.sort(key=lambda s: s.started, reverse=True)
    return sessions


def spawn_agent(
    task_id: str,
    prompt: str,
    agent_type: Literal["general", "explore", "plan", "execute"] = "general",
    backend: Literal["gptme", "claude", "codex"] = "gptme",
    background: bool = False,
    workspace: Optional[Path] = None,
    timeout: int = 600,
    model: Optional[str] = None,
    clear_keys: Optional[bool] = None,
) -> AgentSession:
    """Spawn a sub-agent to work on a task.

    Args:
        task_id: The task ID being worked on
        prompt: The prompt/instructions for the agent
        agent_type: Type of agent (general, explore, plan, execute)
        backend: Which backend to use (gptme or claude)
        background: If True, run in tmux session
        workspace: Working directory for the agent
        timeout: Timeout in seconds (for foreground only)
        model: Model to use (e.g. openrouter/moonshotai/kimi-k2.5@moonshotai)
        clear_keys: If True, explicitly unset API keys (useful for backends
            with their own auth like Claude Code/Codex). If None (default),
            auto-detects based on backend (True for claude, False for gptme).

    Returns:
        AgentSession with status and session_id
    """
    if workspace is None:
        workspace = Path.cwd()

    session_id = f"agent_{uuid.uuid4().hex[:8]}"
    sessions_dir = get_sessions_dir(workspace)
    output_file = sessions_dir / f"{session_id}.output"

    session = AgentSession(
        session_id=session_id,
        task_id=task_id,
        agent_type=agent_type,
        backend=backend,
        started=datetime.now(timezone.utc).isoformat(),
        status="running",
        output_file=str(output_file),
    )

    if background:
        # Run in tmux for background execution
        tmux_name = f"gptodo_{session_id}"
        session.tmux_session = tmux_name

        # Escape shell arguments to prevent injection
        safe_prompt = shlex.quote(prompt)
        safe_output = shlex.quote(str(output_file))

        if backend == "gptme":
            model_arg = f"--model {shlex.quote(model)}" if model else ""
            shell_cmd = f'gptme -n {model_arg} {safe_prompt} > {safe_output} 2>&1; echo "EXIT_CODE=$?" >> {safe_output}'
        else:
            # Claude backend doesn't support model selection
            shell_cmd = f'claude -p --dangerously-skip-permissions --tools default {safe_prompt} > {safe_output} 2>&1; echo "EXIT_CODE=$?" >> {safe_output}'

        # Build environment exports for critical API keys
        # These may not be inherited by tmux detached sessions
        #
        # NOTE: For backends with their own auth (claude, codex), we can
        # explicitly clear API keys to ensure they use OAuth subscription
        # (flat-fee) rather than API keys (metered).
        # If ANTHROPIC_API_KEY is in the env, Claude Code uses it, bypassing
        # the subscription and hitting API rate limits.
        env_exports = []
        env_unsets = []  # Variables to explicitly unset

        # Base environment variables needed by all backends
        base_vars = ["PATH", "HOME"]

        # API keys that backends might use
        api_key_vars = [
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
        ]

        # Determine whether to clear API keys (auto-detect based on backend if not specified)
        should_clear_keys = (
            clear_keys if clear_keys is not None else (backend in ("claude", "codex"))
        )

        if should_clear_keys:
            # For backends with their own auth: export only GPTME_* config vars
            # and explicitly unset API keys to prevent interference
            api_vars = ["GPTME_MODEL"]
            env_unsets = api_key_vars  # Will be unset in the shell
        else:
            # For gptme backend: export all API keys
            api_vars = api_key_vars + ["GPTME_MODEL"]

        for key in base_vars + api_vars:
            value = os.environ.get(key)
            if value:
                # Export the variable inside the tmux session
                env_exports.append(f"export {key}={shlex.quote(value)}")

        # Build env setup: unsets first (if any), then exports
        env_commands = []
        if env_unsets:
            env_commands.append(f"unset {' '.join(env_unsets)}")
        env_commands.extend(env_exports)
        env_setup = "; ".join(env_commands) + "; " if env_commands else ""

        # Use bash -l to ensure login shell behavior (sources .profile/.bashrc)
        # This ensures PATH and other environment variables are properly set
        # Note: workspace must be shell-quoted inside the command to handle paths with spaces
        safe_workspace = shlex.quote(str(workspace))
        full_cmd = f"bash -l -c {shlex.quote(f'cd {safe_workspace} && {env_setup}{shell_cmd}')}"

        # Start tmux session
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, full_cmd],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            session.status = "failed"
            session.error = f"Failed to start tmux: {result.stderr}"

        save_session(session, workspace)
        return session

    # Foreground execution
    if backend == "gptme":
        cmd = ["gptme", "-n"]
        if model:
            cmd.extend(["--model", model])
        cmd.append(prompt)
    else:
        # Claude backend doesn't support model selection
        cmd = ["claude", "-p", "--dangerously-skip-permissions", "--tools", "default", prompt]

    # Build environment for subprocess
    # If clear_keys is enabled, create modified environment without API keys
    should_clear_keys_fg = (
        clear_keys if clear_keys is not None else (backend in ("claude", "codex"))
    )
    if should_clear_keys_fg:
        # Copy environment and remove API keys
        env = os.environ.copy()
        for key in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"]:
            env.pop(key, None)
    else:
        env = None  # Use inherited environment

    try:
        result = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        # Save output
        output_file.write_text(result.stdout + "\n" + result.stderr)

        session.status = "completed" if result.returncode == 0 else "failed"
        session.completed_at = datetime.now(timezone.utc).isoformat()
        if result.returncode != 0:
            session.error = f"Exit code: {result.returncode}"

    except subprocess.TimeoutExpired:
        session.status = "failed"
        session.error = f"Timeout after {timeout}s"
    except Exception as e:
        session.status = "failed"
        session.error = str(e)

    save_session(session, workspace)
    return session


def check_session(session_id: str, workspace: Optional[Path] = None) -> Optional[AgentSession]:
    """Check status of a session and update if completed.

    For background sessions, checks if tmux session still exists and
    reads any available output.
    """
    session = load_session(session_id, workspace)
    if session is None:
        return None

    if session.status != "running":
        return session

    if session.tmux_session:
        # Check if tmux session still exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", session.tmux_session],
            capture_output=True,
        )

        if result.returncode != 0:
            # Session ended, check output for exit code
            session.status = "completed"
            session.completed_at = datetime.now(timezone.utc).isoformat()

            if session.output_file and Path(session.output_file).exists():
                output = Path(session.output_file).read_text()
                if "EXIT_CODE=0" in output:
                    session.status = "completed"
                elif "EXIT_CODE=" in output:
                    session.status = "failed"
                    session.error = "Non-zero exit code"

            save_session(session, workspace)

    return session


def get_session_output(session_id: str, workspace: Optional[Path] = None) -> str:
    """Get output from a session."""
    session = load_session(session_id, workspace)
    if session is None:
        return f"Session {session_id} not found"

    output = ""

    # For background sessions, also capture live tmux output
    if session.tmux_session and session.status == "running":
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session.tmux_session],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            output += f"=== Live Output ===\n{result.stdout}\n"

    # Also read from output file if exists
    if session.output_file and Path(session.output_file).exists():
        output += f"=== Saved Output ===\n{Path(session.output_file).read_text()}\n"

    return output or "No output available"


def kill_session(session_id: str, workspace: Optional[Path] = None) -> bool:
    """Kill a running session."""
    session = load_session(session_id, workspace)
    if session is None:
        return False

    if session.status != "running":
        return False

    if session.tmux_session:
        result = subprocess.run(
            ["tmux", "kill-session", "-t", session.tmux_session],
            capture_output=True,
        )

        session.status = "killed"
        session.completed_at = datetime.now(timezone.utc).isoformat()
        save_session(session, workspace)
        return result.returncode == 0

    return False


def cleanup_sessions(
    workspace: Optional[Path] = None,
    older_than_hours: int = 24,
) -> int:
    """Remove old session files.

    Returns count of sessions cleaned up.
    """
    sessions = list_sessions(workspace)
    count = 0
    sessions_dir = get_sessions_dir(workspace)
    cutoff = datetime.now(timezone.utc).timestamp() - (older_than_hours * 3600)

    for session in sessions:
        if session.status in ("completed", "failed", "killed"):
            started = datetime.fromisoformat(session.started.replace("Z", "+00:00"))
            if started.timestamp() < cutoff:
                # Remove session file
                session_file = sessions_dir / f"{session.session_id}.json"
                if session_file.exists():
                    session_file.unlink()
                    count += 1

                # Remove output file
                if session.output_file:
                    output_path = Path(session.output_file)
                    if output_path.exists():
                        output_path.unlink()

    return count
