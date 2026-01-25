"""Execution utilities for running gptme."""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Global log directory (not in workspace to prevent Issue #151 recursive grep)
GLOBAL_LOG_DIR = Path.home() / ".cache" / "gptme" / "logs"
GLOBAL_LOG_DIR.mkdir(parents=True, exist_ok=True)


class ExecutionResult:
    """Result from gptme execution."""

    def __init__(self, exit_code: int, timed_out: bool = False):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.success = exit_code == 0


def execute_gptme(
    prompt: str,
    workspace: Path,
    timeout: int,
    non_interactive: bool = True,
    shell_timeout: int = 120,
    env: Optional[dict] = None,
    run_type: str = "run",
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

    Returns:
        ExecutionResult with exit code and status
    """
    # Create global log file for this run
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = GLOBAL_LOG_DIR / f"{run_type}-{timestamp}.log"
    # Create temporary prompt file
    prompt_file = workspace / f".gptme-prompt-{os.getpid()}.txt"
    prompt_file.write_text(prompt)

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

        # Use tee to stream output to both terminal and log file
        # This gives us real-time journald logging AND complete log file
        cmd_with_tee = f"{' '.join(cmd)} 2>&1 | tee '{log_file}'"

        # Write header to log file first
        with log_file.open("w") as f:
            f.write(f"=== {run_type} run at {timestamp} ===\n")
            f.write(f"Working directory: {workspace}\n")
            f.write(f"Command: {' '.join(cmd)}\n")
            f.write(f"Timeout: {timeout}s\n")
            f.write(f"Shell timeout: {shell_timeout}s\n\n")
            f.write("=== Output ===\n")

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

            return ExecutionResult(exit_code=result.returncode)

        except subprocess.TimeoutExpired:
            # Log timeout to file (append to preserve tee output)
            with log_file.open("a") as f:
                f.write("\n=== Execution timed out ===\n")
                f.write(f"Status: TIMED OUT after {timeout}s\n")

            print(f"ERROR: Execution timed out after {timeout}s", file=sys.stderr)
            return ExecutionResult(exit_code=124, timed_out=True)

    finally:
        # Clean up prompt file
        if prompt_file.exists():
            prompt_file.unlink()
