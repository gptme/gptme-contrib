"""
Tool bridge for dispatching tasks to gptme subagents.

When the voice model needs to interact with the codebase, it dispatches
a natural language task to a gptme subagent that runs non-interactively
in the workspace. The subagent writes its response to a temp file which
we read back as the clean result.

Tasks run asynchronously — the function call returns immediately so the
voice conversation continues, and the result is injected into the
conversation when ready.
"""

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Max output to return (avoid overwhelming the realtime API)
_MAX_OUTPUT_LEN = 2000
_IGNORABLE_ERROR_LINES = {
    "Warning: Input is not a terminal (fd=0).",
}

# Appended to each task so the subagent writes a clean response file
_RESPONSE_SUFFIX = (
    "\n\nIMPORTANT: Write your final response (concise summary of findings, "
    "suitable for reading aloud in a voice conversation) to the file: {response_file}"
)


@dataclass
class ToolResult:
    """Result from a subagent execution."""

    success: bool
    output: str
    error: str | None = None


class GptmeToolBridge:
    """
    Bridge between OpenAI Realtime function calls and gptme subagents.

    Dispatches tasks to gptme running in non-interactive mode,
    which has access to shell, file I/O, python, and other tools.
    Tasks run in the background so the voice conversation isn't blocked.
    """

    # Model presets for subagent speed/quality tradeoff
    MODEL_FAST = "openrouter/anthropic/claude-haiku-4.5"
    MODEL_SMART = None  # Use gptme default model

    def __init__(
        self,
        gptme_path: str = "gptme",
        timeout: int = 300,
        workspace: str | None = None,
        on_result: Callable[[str], Awaitable[None]] | None = None,
        on_hangup: Callable[[str | None], Awaitable[None]] | None = None,
    ):
        self.gptme_path = os.environ.get("GPTME_VOICE_SUBAGENT_PATH") or gptme_path
        self.timeout = timeout
        self.workspace = workspace
        self.on_result = on_result
        self.on_hangup = on_hangup
        env_model = os.environ.get("GPTME_VOICE_SUBAGENT_MODEL")
        self.model_fast = env_model or self.MODEL_FAST
        self.model_smart = env_model or self.MODEL_SMART
        self._pending_tasks: dict[str, asyncio.Task] = {}
        self._task_counter = 0

    @staticmethod
    def _extract_error_text(stdout: str, stderr: str, output: str) -> str:
        stderr_lines = [
            line.strip()
            for line in stderr.splitlines()
            if line.strip() and line.strip() not in _IGNORABLE_ERROR_LINES
        ]
        if stderr_lines:
            return "\n".join(stderr_lines)

        stdout_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        for needle in ("Error code:", "API error:", "invalid_request_error", "ERROR"):
            for line in reversed(stdout_lines):
                if needle.lower() in line.lower():
                    return line
        if output:
            return output
        if stdout_lines:
            return stdout_lines[-1]
        fallback_stderr = stderr.strip()
        if fallback_stderr:
            return fallback_stderr
        return ""

    async def _run_subagent(self, task_id: str, task: str, mode: str = "smart") -> None:
        """Run a subagent in the background and inject result when done."""
        result = await self._execute(task, mode=mode)

        if result.success:
            response_text = result.output
        else:
            response_text = f"Subagent error: {result.error or 'Unknown error'}"

        logger.info(f"Task {task_id} complete: {response_text[:100]}...")

        # Inject result into conversation
        if self.on_result:
            await self.on_result(response_text)

        # Clean up
        self._pending_tasks.pop(task_id, None)

    async def _execute(self, task: str, mode: str = "smart") -> ToolResult:
        """Execute a gptme subagent and return the result."""
        with tempfile.NamedTemporaryFile(
            prefix="gptme-voice-", suffix=".md", delete=False
        ) as tf:
            response_file = Path(tf.name)
        augmented_task = task + _RESPONSE_SUFFIX.format(response_file=response_file)

        model = self.model_fast if mode == "fast" else self.model_smart
        logger.info(f"Dispatching subagent ({mode}): {task}")
        logger.debug(f"Response file: {response_file}")

        cmd = [
            self.gptme_path,
            "--non-interactive",
            "--context",
            "files",
        ]
        if model:
            cmd += ["--model", model, "--tool-format", "tool"]
        cmd.append(augmented_task)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Subagent timed out after {self.timeout}s",
                )

            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            # Read the response file if it exists
            if response_file.exists():
                output = response_file.read_text().strip()
                if output:
                    logger.info(
                        f"Subagent response ({len(output)} chars): {output[:200]}..."
                    )
                else:
                    output = stdout_text
                    logger.warning(
                        "Subagent wrote an empty response file, using stdout"
                    )
            else:
                # Fall back to stdout if no response file was written
                output = stdout_text
                logger.warning("Subagent did not write response file, using stdout")

            error = self._extract_error_text(stdout_text, stderr_text, output)

            # Truncate long output
            if len(output) > _MAX_OUTPUT_LEN:
                output = (
                    output[:_MAX_OUTPUT_LEN]
                    + f"\n... (truncated, {len(output)} total chars)"
                )

            if process.returncode != 0:
                logger.error(
                    f"Subagent failed (exit {process.returncode}): {error or output[:200]}"
                )
                return ToolResult(
                    success=False,
                    output=output,
                    error=error or f"Exit code {process.returncode}",
                )

            return ToolResult(success=True, output=output, error=None)

        except FileNotFoundError:
            return ToolResult(
                success=False, output="", error=f"gptme not found at {self.gptme_path}"
            )
        except Exception as e:
            logger.error(f"Subagent error: {e}")
            return ToolResult(success=False, output="", error=str(e))
        finally:
            response_file.unlink(missing_ok=True)

    async def handle_function_call(self, name: str, arguments: dict) -> dict:
        """Handle an OpenAI function call.

        Dispatches subagent tasks asynchronously and returns immediately.
        """
        if name == "subagent":
            task = arguments.get("task", "")
            if not task:
                return {"error": "No task provided"}

            mode = arguments.get("mode", "smart")
            if mode not in ("fast", "smart"):
                mode = "smart"

            # Assign task ID and dispatch in background
            self._task_counter += 1
            task_id = f"task-{self._task_counter}"

            bg_task = asyncio.create_task(self._run_subagent(task_id, task, mode=mode))
            self._pending_tasks[task_id] = bg_task

            return {
                "status": "dispatched",
                "task_id": task_id,
                "message": f"Working on it: {task}",
            }

        if name == "hangup":
            reason = arguments.get("reason") or None
            logger.info("Hangup requested (reason=%s)", reason or "<none>")
            if self.on_hangup is None:
                return {
                    "status": "not_supported",
                    "message": (
                        "Hang-up is not wired up on this server; "
                        "the call will end when the caller hangs up."
                    ),
                }
            # Fire the teardown in the background so the model still gets a
            # synchronous function-call response and can finish speaking.
            asyncio.create_task(self.on_hangup(reason))
            return {
                "status": "hanging_up",
                "message": "Ending the call shortly.",
            }

        return {"error": f"Unknown function: {name}"}
