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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Max task-description length to echo back when reporting status
_MAX_TASK_PREVIEW = 120

# Max output to return (avoid overwhelming the realtime API)
_MAX_OUTPUT_LEN = 2000
_IGNORABLE_ERROR_LINES = {
    "Warning: Input is not a terminal (fd=0).",
}
_DEFAULT_TRANSCRIPT_TAIL_TURNS = 8
_DEFAULT_TRANSCRIPT_TAIL_CHARS = 1_600

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


@dataclass
class PendingTask:
    """Metadata for an in-flight subagent dispatch."""

    task: asyncio.Task
    description: str
    mode: str
    started_at: float
    model: str | None = None
    last_output: str = field(default="")
    process_started_at: float | None = None
    first_output_at: float | None = None
    last_output_at: float | None = None
    completed_at: float | None = None
    returncode: int | None = None


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
        on_handoff: Callable[[str, str, str | None], Awaitable[dict]] | None = None,
        transcript_provider: Callable[[], list[object]] | None = None,
    ):
        self.gptme_path = os.environ.get("GPTME_VOICE_SUBAGENT_PATH") or gptme_path
        self.timeout = timeout
        self.workspace = workspace
        self.on_result = on_result
        self.on_hangup = on_hangup
        self.on_handoff = on_handoff
        self.transcript_provider = transcript_provider
        legacy_env_model = os.environ.get("GPTME_VOICE_SUBAGENT_MODEL")
        env_model_fast = os.environ.get("GPTME_VOICE_SUBAGENT_MODEL_FAST")
        env_model_smart = os.environ.get("GPTME_VOICE_SUBAGENT_MODEL_SMART")
        self.model_fast = env_model_fast or legacy_env_model or self.MODEL_FAST
        self.model_smart = env_model_smart or legacy_env_model or self.MODEL_SMART
        self.transcript_tail_turns = self._parse_env_int(
            "GPTME_VOICE_SUBAGENT_TRANSCRIPT_TAIL_TURNS",
            default=_DEFAULT_TRANSCRIPT_TAIL_TURNS,
            minimum=0,
        )
        self.transcript_tail_chars = self._parse_env_int(
            "GPTME_VOICE_SUBAGENT_TRANSCRIPT_TAIL_CHARS",
            default=_DEFAULT_TRANSCRIPT_TAIL_CHARS,
            minimum=0,
        )
        self._pending_tasks: dict[str, PendingTask] = {}
        self._task_counter = 0
        self._completed_timings: list[dict[str, object]] = []

    @staticmethod
    def _parse_env_int(name: str, *, default: int, minimum: int = 0) -> int:
        value = os.environ.get(name)
        if value is None:
            return default
        try:
            return max(minimum, int(value))
        except ValueError:
            logger.warning("%s=%r is not an integer; using %s", name, value, default)
            return default

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
        # Don't fall back to raw stderr here: if we got this far, the primary
        # stderr filter already excluded ignorable lines (e.g. the non-TTY
        # warning from prompt_toolkit). Returning raw stderr would re-surface
        # exactly those filtered lines as the "error" reported to the user.
        # Returning "" lets the caller fall back to the exit-code message.
        return ""

    @staticmethod
    def _round_seconds(value: float | None) -> float | None:
        if value is None:
            return None
        return round(max(0.0, value), 1)

    @staticmethod
    def _extract_transcript_turn(turn: object) -> tuple[str, str] | None:
        if isinstance(turn, dict):
            role = turn.get("role")
            text = turn.get("text")
        else:
            role = getattr(turn, "role", None)
            text = getattr(turn, "text", None)

        if not isinstance(role, str) or not isinstance(text, str):
            return None

        cleaned = text.strip()
        if not cleaned:
            return None

        return role.strip(), cleaned

    @staticmethod
    def _truncate_transcript_line(role: str, text: str, max_chars: int) -> str:
        prefix = f"{role.title()}: "
        if len(prefix) + len(text) <= max_chars:
            return prefix + text

        remaining = max_chars - len(prefix)
        if remaining <= 0:
            return (prefix + text)[-max_chars:]
        if remaining <= 3:
            return prefix + text[-remaining:]
        return prefix + "..." + text[-(remaining - 3) :]

    def _build_transcript_tail(self) -> tuple[str, int] | None:
        if (
            self.transcript_provider is None
            or self.transcript_tail_turns <= 0
            or self.transcript_tail_chars <= 0
        ):
            return None

        try:
            raw_turns = list(self.transcript_provider() or [])
        except Exception as exc:
            logger.warning(
                "Failed to fetch transcript tail for voice subagent: %s", exc
            )
            return None

        formatted_lines: list[str] = []
        total_chars = 0
        included_turns = 0

        for raw_turn in reversed(raw_turns[-self.transcript_tail_turns :]):
            turn = self._extract_transcript_turn(raw_turn)
            if turn is None:
                continue
            role, text = turn
            line = self._truncate_transcript_line(
                role, text, self.transcript_tail_chars
            )
            line_chars = len(line) + (1 if formatted_lines else 0)
            if (
                formatted_lines
                and total_chars + line_chars > self.transcript_tail_chars
            ):
                break
            formatted_lines.append(line)
            total_chars += line_chars
            included_turns += 1

        if not formatted_lines:
            return None

        return "\n".join(reversed(formatted_lines)), included_turns

    def _timing_breakdown(
        self,
        entry: PendingTask,
        now: float | None = None,
    ) -> dict[str, float]:
        reference_time = entry.completed_at or now or time.monotonic()
        result: dict[str, float] = {}
        dispatch_to_spawn = None
        if entry.process_started_at is not None:
            dispatch_to_spawn = self._round_seconds(
                entry.process_started_at - entry.started_at
            )
        if dispatch_to_spawn is not None:
            result["dispatch_to_spawn_seconds"] = dispatch_to_spawn

        if entry.process_started_at is not None:
            spawn_elapsed = self._round_seconds(
                reference_time - entry.process_started_at
            )
            if spawn_elapsed is not None:
                result["spawn_elapsed_seconds"] = spawn_elapsed

        if entry.process_started_at is not None and entry.first_output_at is not None:
            first_output = self._round_seconds(
                entry.first_output_at - entry.process_started_at
            )
            if first_output is not None:
                result["spawn_to_first_output_seconds"] = first_output

        if entry.first_output_at is not None:
            output_elapsed = self._round_seconds(reference_time - entry.first_output_at)
            if output_elapsed is not None:
                result["output_elapsed_seconds"] = output_elapsed

        if entry.last_output_at is not None:
            last_output_age = self._round_seconds(reference_time - entry.last_output_at)
            if last_output_age is not None:
                result["last_output_age_seconds"] = last_output_age

        if entry.completed_at is not None:
            total = self._round_seconds(entry.completed_at - entry.started_at)
            if total is not None:
                result["total_seconds"] = total

        return result

    def _pending_stage(self, entry: PendingTask) -> str:
        if entry.completed_at is not None:
            return "completed"
        if entry.process_started_at is None:
            return "queued"
        if entry.first_output_at is None:
            return "starting"
        return "running"

    def _log_timing_summary(self, task_id: str, entry: PendingTask) -> None:
        timings = self._timing_breakdown(entry)
        if not timings:
            return

        labels = (
            ("dispatch_to_spawn_seconds", "dispatch->spawn"),
            ("spawn_to_first_output_seconds", "spawn->first_output"),
            ("spawn_elapsed_seconds", "spawn->done"),
            ("output_elapsed_seconds", "first_output->done"),
            ("last_output_age_seconds", "quiet_tail"),
            ("total_seconds", "total"),
        )
        parts = []
        for key, label in labels:
            value = timings.get(key)
            if value is not None:
                parts.append(f"{label}={value:.1f}s")

        if entry.returncode is not None:
            parts.append(f"exit={entry.returncode}")

        logger.info(
            "Task %s timings (%s, model=%s): %s",
            task_id,
            entry.mode,
            entry.model or "default",
            ", ".join(parts),
        )

        self._completed_timings.append(
            self._build_timing_record(task_id, entry, timings)
        )

    def _build_timing_record(
        self,
        task_id: str,
        entry: PendingTask,
        timings: dict[str, float],
    ) -> dict[str, object]:
        description = entry.description or ""
        if len(description) > _MAX_TASK_PREVIEW:
            description = description[: _MAX_TASK_PREVIEW - 1] + "…"
        record: dict[str, object] = {
            "task_id": task_id,
            "mode": entry.mode,
            "model": entry.model,
            "task_preview": description,
            "returncode": entry.returncode,
            "timings": dict(timings),
        }
        return record

    def get_timings(self) -> list[dict[str, object]]:
        """Return a copy of timing records for completed subagent runs.

        Each entry is deep-copied one level so callers can mutate without
        affecting the bridge's internal state.
        """
        copies: list[dict[str, object]] = []
        for record in self._completed_timings:
            copy = dict(record)
            inner = record.get("timings")
            if isinstance(inner, dict):
                copy["timings"] = dict(inner)
            copies.append(copy)
        return copies

    async def _run_subagent(self, task_id: str, task: str, mode: str = "smart") -> None:
        """Run a subagent in the background and inject result when done."""
        pending = self._pending_tasks.get(task_id)

        def _on_started(started_at: float) -> None:
            if pending is not None:
                pending.process_started_at = started_at

        def _on_progress(line: str, emitted_at: float) -> None:
            if pending is not None:
                pending.last_output = line
                pending.last_output_at = emitted_at
                if pending.first_output_at is None:
                    pending.first_output_at = emitted_at

        def _on_completed(returncode: int, completed_at: float) -> None:
            if pending is not None:
                pending.completed_at = completed_at
                pending.returncode = returncode

        model = self.model_fast if mode == "fast" else self.model_smart

        if pending is not None:
            pending.model = model

        try:
            result = await self._execute(
                task,
                mode=mode,
                on_started=_on_started,
                on_progress=_on_progress,
                on_completed=_on_completed,
            )
        except asyncio.CancelledError:
            logger.info(f"Task {task_id} cancelled")
            if self.on_result:
                await self.on_result(
                    f"Subagent task {task_id} was cancelled before it finished."
                )
            self._pending_tasks.pop(task_id, None)
            raise

        if result.success:
            response_text = result.output
        else:
            response_text = f"Subagent error: {result.error or 'Unknown error'}"

        if pending is not None:
            self._log_timing_summary(task_id, pending)

        logger.info(f"Task {task_id} complete: {response_text[:100]}...")

        # Inject result into conversation
        if self.on_result:
            await self.on_result(response_text)

        # Clean up
        self._pending_tasks.pop(task_id, None)

    async def _execute(
        self,
        task: str,
        mode: str = "smart",
        on_started: Callable[[float], None] | None = None,
        on_progress: Callable[[str, float], None] | None = None,
        on_completed: Callable[[int, float], None] | None = None,
    ) -> ToolResult:
        """Execute a gptme subagent and return the result."""
        with tempfile.NamedTemporaryFile(
            prefix="gptme-voice-", suffix=".md", delete=False
        ) as tf:
            response_file = Path(tf.name)
        transcript_tail_file: Path | None = None
        augmented_task = task + _RESPONSE_SUFFIX.format(response_file=response_file)

        model = self.model_fast if mode == "fast" else self.model_smart
        logger.info(
            f"Dispatching subagent ({mode}, model={model or 'default'}): {task}"
        )
        logger.debug(f"Response file: {response_file}")

        cmd = [
            self.gptme_path,
            "--non-interactive",
            "--context",
            "files",
        ]
        # Keep project prompt files but skip context_cmd. This avoids pulling in
        # scripts/context.sh while still giving the subagent its runtime rules.
        if model:
            cmd += ["--model", model, "--tool-format", "tool"]
        transcript_tail = self._build_transcript_tail()
        if transcript_tail is not None:
            transcript_tail_text, transcript_tail_turns = transcript_tail
            with tempfile.NamedTemporaryFile(
                prefix="gptme-voice-transcript-", suffix=".txt", delete=False
            ) as tf:
                transcript_tail_file = Path(tf.name)
                tf.write(transcript_tail_text.encode("utf-8"))
            cmd += [
                "--transcript-tail-turns",
                str(transcript_tail_turns),
                "--transcript-tail-file",
                str(transcript_tail_file),
            ]
        cmd.append(augmented_task)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            if on_started:
                on_started(time.monotonic())

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            async def _read_stdout() -> None:
                assert process.stdout is not None
                async for raw in process.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        stdout_lines.append(line)
                        if on_progress:
                            on_progress(line, time.monotonic())

            async def _read_stderr() -> None:
                assert process.stderr is not None
                async for raw in process.stderr:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        stderr_lines.append(line)

            try:
                await asyncio.wait_for(
                    asyncio.gather(_read_stdout(), _read_stderr(), process.wait()),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Subagent timed out after {self.timeout}s",
                )
            except asyncio.CancelledError:
                process.kill()
                raise

            stdout_text = "\n".join(stdout_lines).strip()
            stderr_text = "\n".join(stderr_lines).strip()

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

            if on_completed:
                on_completed(process.returncode, time.monotonic())

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
            if transcript_tail_file is not None:
                transcript_tail_file.unlink(missing_ok=True)

    def _describe_pending(self, task_id: str, entry: PendingTask) -> dict:
        description = entry.description
        if len(description) > _MAX_TASK_PREVIEW:
            description = description[:_MAX_TASK_PREVIEW].rstrip() + "..."
        elapsed = max(0.0, time.monotonic() - entry.started_at)
        result: dict = {
            "task_id": task_id,
            "task": description,
            "mode": entry.mode,
            "elapsed_seconds": round(elapsed, 1),
            "stage": self._pending_stage(entry),
        }
        if entry.model:
            result["model"] = entry.model
        timings = self._timing_breakdown(entry)
        if timings:
            result["timings"] = timings
        if entry.last_output:
            result["last_output"] = entry.last_output[:200]
        return result

    async def _cancel_task(self, task_id: str, entry: PendingTask) -> dict:
        entry.task.cancel()
        try:
            await entry.task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - report anything else back
            logger.warning(f"Task {task_id} raised during cancel: {e}")
        self._pending_tasks.pop(task_id, None)
        return {"task_id": task_id, "cancelled": True}

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
            model = self.model_fast if mode == "fast" else self.model_smart

            # Assign task ID and dispatch in background
            self._task_counter += 1
            task_id = f"task-{self._task_counter}"

            bg_task = asyncio.create_task(self._run_subagent(task_id, task, mode=mode))
            self._pending_tasks[task_id] = PendingTask(
                task=bg_task,
                description=task,
                mode=mode,
                started_at=time.monotonic(),
                model=model,
            )

            return {
                "status": "dispatched",
                "task_id": task_id,
                "message": f"Working on it: {task}",
            }

        if name == "subagent_status":
            pending = [
                self._describe_pending(tid, entry)
                for tid, entry in self._pending_tasks.items()
                if not entry.task.done()
            ]
            return {
                "status": "ok",
                "pending_count": len(pending),
                "pending": pending,
            }

        if name == "subagent_cancel":
            task_id_arg: str | None = arguments.get("task_id")
            if task_id_arg:
                entry = self._pending_tasks.get(task_id_arg)
                if entry is None or entry.task.done():
                    return {
                        "status": "not_found",
                        "task_id": task_id_arg,
                        "message": (
                            f"No pending subagent task with id {task_id_arg}. "
                            "Use subagent_status to list pending tasks."
                        ),
                    }
                result = await self._cancel_task(task_id_arg, entry)
                return {"status": "cancelled", **result}

            # No task_id — cancel all pending
            targets = [
                (tid, entry)
                for tid, entry in list(self._pending_tasks.items())
                if not entry.task.done()
            ]
            if not targets:
                return {
                    "status": "no_pending",
                    "message": "No subagent tasks are currently running.",
                }
            cancelled = []
            for tid, entry in targets:
                result = await self._cancel_task(tid, entry)
                cancelled.append(result)
            return {
                "status": "cancelled_all",
                "cancelled_count": len(cancelled),
                "cancelled": cancelled,
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

        if name == "handoff_to_agent":
            to_agent = arguments.get("to_agent", "")
            reason = arguments.get("reason", "")
            context_summary = arguments.get("context_summary") or None
            logger.info(
                "Handoff requested (to_agent=%s, reason=%s)",
                to_agent,
                reason or "<none>",
            )
            if self.on_handoff is None:
                return {
                    "status": "not_supported",
                    "message": (
                        "Handoff is not configured on this server. "
                        "Set GPTME_VOICE_HANDOFF_DIR to enable cross-agent transfers."
                    ),
                }
            result = await self.on_handoff(to_agent, reason, context_summary)
            return result

        return {"error": f"Unknown function: {name}"}
