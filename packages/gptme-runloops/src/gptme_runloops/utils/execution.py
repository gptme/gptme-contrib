"""Execution utilities for running gptme."""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Global log directory (not in workspace to prevent Issue #151 recursive grep)
GLOBAL_LOG_DIR = Path.home() / ".cache" / "gptme" / "logs"
GLOBAL_LOG_DIR.mkdir(parents=True, exist_ok=True)


class ExecutionResult:
    """Result from gptme execution."""

    def __init__(
        self,
        exit_code: int,
        timed_out: bool = False,
        trajectory_path: Path | None = None,
        tmpdir: Path | None = None,
    ):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.success = exit_code == 0
        # Path to conversation.jsonl written by this gptme session (if any).
        # Populated when GPTME_LOGS_HOME isolation is used — lets callers
        # record the session via post_session() without fragile mtime searches.
        self.trajectory_path = trajectory_path
        # Temporary directory holding the isolated gptme session logs.
        # Owned by this result; caller must call cleanup_tmpdir() after
        # post_session() has ingested the trajectory, or use the context
        # manager form.
        self.tmpdir = tmpdir

    def cleanup_tmpdir(self) -> None:
        """Delete the isolated gptme session logs directory.

        Call this after post_session() has processed trajectory_path to avoid
        accumulating stale tmpdir entries across many runs.
        """
        if self.tmpdir and self.tmpdir.exists():
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None


def execute_gptme(
    prompt: str,
    workspace: Path,
    timeout: int,
    non_interactive: bool = True,
    shell_timeout: int = 120,
    env: dict | None = None,
    run_type: str = "run",
    tools: str | None = None,
    model: str | None = None,
    tool_format: str | None = None,
) -> ExecutionResult:
    """Execute gptme with the given prompt.

    Args:
        prompt: Prompt text to pass to gptme
        workspace: Working directory for execution
        timeout: Maximum execution time in seconds
        non_interactive: Run in non-interactive mode
        shell_timeout: Shell command timeout in seconds
        env: Additional environment variables
        run_type: Type of run (for log file naming)
        tools: Tool allowlist string (e.g. "gptodo,save,append")
        model: Model override (e.g. "openai-subscription/gpt-5.3-codex")
        tool_format: Tool format override (markdown/xml/tool)

    Returns:
        ExecutionResult with exit code and status
    """
    # Create global log file for this run
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = GLOBAL_LOG_DIR / f"{run_type}-{timestamp}.log"
    # Create temporary prompt file
    prompt_file = workspace / f".gptme-prompt-{os.getpid()}.txt"
    prompt_file.write_text(prompt)

    # Isolate gptme session logs to a private tmpdir so trajectory discovery is
    # reliable. When gptme runs with --workspace pointing to a dir that has an
    # [agent] section in gptme.toml, it ignores --name and creates random
    # YYYY-MM-DD-adjective-noun "petname" directories instead — breaking any
    # pattern-based lookup. GPTME_LOGS_HOME redirects all logs for this process
    # to a known private dir, making the conversation.jsonl always findable.
    gptme_logs_dir = Path(tempfile.mkdtemp(prefix="gptme-session-"))

    try:
        # Build gptme command
        # Find gptme in PATH (typically pipx-managed)
        gptme_path = shutil.which("gptme")
        if not gptme_path:
            raise RuntimeError(
                "gptme not found in PATH. Install with: pipx install gptme"
            )
        cmd = [gptme_path]
        if non_interactive:
            cmd.append("--non-interactive")

        if model:
            cmd.extend(["--model", model])

        if tool_format:
            cmd.extend(["--tool-format", tool_format])

        if tools:
            cmd.extend(["--tools", tools])

        # this line is essential for the prompt file path to not be mistaken for a command
        cmd.append("'Here is the prompt to follow:'")

        # mentioning the file here includes its contents in the initial message
        cmd.append(str(prompt_file))

        # Set up environment
        run_env = os.environ.copy()
        run_env["GPTME_SHELL_TIMEOUT"] = str(shell_timeout)
        run_env["GPTME_CHAT_HISTORY"] = "true"

        if env:
            run_env.update(env)

        # Must be set after caller env merge so it cannot be clobbered.
        run_env["GPTME_LOGS_HOME"] = str(gptme_logs_dir)

        # Use tee to stream output to both terminal and log file
        # This gives us real-time journald logging AND complete log file
        # Use shlex.join for proper escaping to prevent command injection
        cmd_with_tee = f"{shlex.join(cmd)} 2>&1 | tee {shlex.quote(str(log_file))}"

        # Write header to log file first
        with log_file.open("w") as f:
            f.write(f"=== {run_type} run at {timestamp} ===\n")
            f.write(f"Working directory: {workspace}\n")
            f.write(f"Command: {' '.join(cmd)}\n")
            f.write(f"Timeout: {timeout}s\n")
            f.write(f"Shell timeout: {shell_timeout}s\n\n")
            f.write("=== Output ===\n")

        trajectory_path: Path | None = None
        timed_out = False

        # Execute with tee - streams to both stdout and log file
        try:
            result = subprocess.run(
                cmd_with_tee,
                shell=True,  # Required for pipe
                cwd=workspace,
                env=run_env,
                timeout=timeout,
            )

            # Append exit code
            with log_file.open("a") as f:
                f.write("\n=== Execution completed ===\n")
                f.write(f"Exit code: {result.returncode}\n")

            exit_code = result.returncode

        except subprocess.TimeoutExpired:
            # Log timeout to file (append to preserve tee output)
            with log_file.open("a") as f:
                f.write("\n=== Execution timed out ===\n")
                f.write(f"Status: TIMED OUT after {timeout}s\n")

            print(f"ERROR: Execution timed out after {timeout}s", file=sys.stderr)
            exit_code = 124
            timed_out = True

        # Discover conversation.jsonl written by this session.
        # gptme creates one session dir per run under GPTME_LOGS_HOME.
        trajs = sorted(gptme_logs_dir.rglob("conversation.jsonl"))
        if trajs:
            selected_traj = trajs[0]
            if len(trajs) > 1:
                with log_file.open("a") as f:
                    f.write("\n=== Multiple conversation.jsonl files found ===\n")
                    f.write(f"Using: {selected_traj}\n")
                    f.write("All candidates:\n")
                    for traj in trajs:
                        f.write(f"- {traj}\n")

            durable_session_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"{run_type}-{timestamp}-session-",
                    dir=GLOBAL_LOG_DIR,
                )
            )
            trajectory_path = durable_session_dir / "conversation.jsonl"
            shutil.copy2(selected_traj, trajectory_path)

        return ExecutionResult(
            exit_code=exit_code,
            timed_out=timed_out,
            trajectory_path=trajectory_path,
            tmpdir=gptme_logs_dir,
        )

    finally:
        # Clean up prompt file and the private GPTME_LOGS_HOME staging dir.
        # trajectory_path points to a durable copy under GLOBAL_LOG_DIR;
        # cleanup_tmpdir() on the result is a no-op since the dir is gone.
        if prompt_file.exists():
            prompt_file.unlink()
        shutil.rmtree(gptme_logs_dir, ignore_errors=True)
