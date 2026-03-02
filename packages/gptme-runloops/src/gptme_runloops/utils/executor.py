"""Backend executor abstraction for run loops.

Provides a pluggable interface for different AI backend tools (gptme, Claude Code, etc.)
so that run loops can work with any backend without modification.
"""

import logging
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from gptme_runloops.utils.execution import ExecutionResult, execute_gptme

logger = logging.getLogger(__name__)


class Executor(ABC):
    """Backend-agnostic execution interface.

    Each executor wraps a specific AI tool (gptme, Claude Code, etc.)
    and provides a uniform interface for running prompts against it.
    """

    name: str  # e.g. 'gptme', 'claude-code'

    @abstractmethod
    def execute(
        self,
        prompt: str,
        workspace: Path,
        timeout: int,
        *,
        model: str | None = None,
        tool_format: str | None = None,
        tools: str | None = None,
        env: dict[str, str] | None = None,
        run_type: str = "run",
        system_prompt_file: Path | None = None,
    ) -> ExecutionResult:
        """Execute a session with the given prompt.

        Args:
            prompt: The prompt text to send to the backend.
            workspace: Working directory for execution.
            timeout: Maximum execution time in seconds.
            model: Model override (backend-specific format).
            tool_format: Tool format override (e.g. markdown/xml/tool).
            tools: Tool allowlist string (e.g. "save,append,read").
            env: Additional environment variables.
            run_type: Type of run (for log file naming).
            system_prompt_file: Path to system prompt file (backend-specific).

        Returns:
            ExecutionResult with exit code and status.
        """
        ...

    def is_available(self) -> bool:
        """Check if this backend is installed and available.

        Returns:
            True if the backend binary is found in PATH.
        """
        return shutil.which(self._binary_name) is not None

    @property
    def _binary_name(self) -> str:
        """Binary name to check for availability."""
        return self.name


class GptmeExecutor(Executor):
    """Executor that wraps the existing gptme backend.

    This is the default executor, preserving all existing behavior
    by delegating to the existing execute_gptme() function.
    """

    name = "gptme"

    def execute(
        self,
        prompt: str,
        workspace: Path,
        timeout: int,
        *,
        model: str | None = None,
        tool_format: str | None = None,
        tools: str | None = None,
        env: dict[str, str] | None = None,
        run_type: str = "run",
        system_prompt_file: Path | None = None,
    ) -> ExecutionResult:
        return execute_gptme(
            prompt=prompt,
            workspace=workspace,
            timeout=timeout,
            non_interactive=True,
            run_type=run_type,
            model=model,
            tool_format=tool_format,
            tools=tools,
            env=env,
        )


class ClaudeCodeExecutor(Executor):
    """Executor for Anthropic's Claude Code CLI.

    Invokes `claude -p` with the given prompt. Handles the CLAUDECODE
    env var to allow nesting (running claude from within a claude session).
    """

    name = "claude-code"

    @property
    def _binary_name(self) -> str:
        return "claude"

    def execute(
        self,
        prompt: str,
        workspace: Path,
        timeout: int,
        *,
        model: str | None = None,
        tool_format: str | None = None,
        tools: str | None = None,
        env: dict[str, str] | None = None,
        run_type: str = "run",
        system_prompt_file: Path | None = None,
    ) -> ExecutionResult:
        # Warn about unsupported parameters — Claude Code CLI doesn't support
        # tool allowlists or tool format flags, so these are silently ignored.
        # This matters for TeamRun: coordinator tool restrictions won't apply.
        if tools:
            logger.warning(
                "ClaudeCodeExecutor: 'tools' parameter is not supported by Claude Code CLI "
                "and will be ignored. Tool restrictions from TeamRun will not be enforced."
            )
        if tool_format:
            logger.warning(
                "ClaudeCodeExecutor: 'tool_format' parameter is not supported by Claude Code CLI "
                "and will be ignored."
            )

        cmd = ["claude", "-p", prompt]

        if system_prompt_file and system_prompt_file.exists():
            cmd += ["--append-system-prompt", system_prompt_file.read_text()]

        if model:
            cmd += ["--model", model]

        # Set up environment — remove CLAUDECODE to allow nesting
        run_env = os.environ.copy()
        run_env.pop("CLAUDECODE", None)
        run_env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        if env:
            run_env.update(env)

        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                env=run_env,
                timeout=timeout,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            # Print output for logging visibility
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            return ExecutionResult(exit_code=result.returncode)
        except subprocess.TimeoutExpired:
            return ExecutionResult(exit_code=124, timed_out=True)


# --- Backend registry ---

EXECUTORS: dict[str, type[Executor]] = {
    "gptme": GptmeExecutor,
    "claude-code": ClaudeCodeExecutor,
}


def get_executor(name: str) -> Executor:
    """Get an executor instance by backend name.

    Args:
        name: Backend name (e.g. 'gptme', 'claude-code').

    Returns:
        Executor instance.

    Raises:
        ValueError: If backend name is unknown.
        RuntimeError: If backend is not available (not installed).
    """
    cls = EXECUTORS.get(name)
    if not cls:
        available = ", ".join(sorted(EXECUTORS.keys()))
        raise ValueError(f"Unknown backend: {name!r}. Available: {available}")
    executor = cls()
    if not executor.is_available():
        raise RuntimeError(
            f"Backend {name!r} is not available "
            f"({executor._binary_name!r} not found in PATH)"
        )
    return executor


def list_backends() -> list[str]:
    """List all registered backend names.

    Returns:
        Sorted list of backend names.
    """
    return sorted(EXECUTORS.keys())
