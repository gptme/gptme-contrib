import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from unittest.mock import AsyncMock

import gptme_voice.realtime.tool_bridge as tool_bridge
import pytest
from gptme_voice.realtime.tool_bridge import (
    _FAST_MODE_SUBPROCESS_TIMEOUT_SECONDS,
    _SMART_MODE_SUBPROCESS_TIMEOUT_SECONDS,
    _TIMEOUT_BINARY_AVAILABLE,
    GptmeToolBridge,
)


@pytest.mark.asyncio
async def test_look_routes_to_vision_bridge() -> None:
    vision = AsyncMock()
    vision.look.return_value = {"status": "ok", "description": "a room"}
    bridge = GptmeToolBridge(workspace="/fake/workspace", vision_bridge=vision)

    result = await bridge.handle_function_call("look", {"prompt": "Who is here?"})

    assert result == {"status": "ok", "description": "a room"}
    vision.look.assert_awaited_once_with("Who is here?")


@pytest.mark.asyncio
async def test_look_without_camera_fails_cleanly() -> None:
    bridge = GptmeToolBridge(workspace="/fake/workspace")
    assert await bridge.handle_function_call("look", {}) == {
        "error": "No camera is connected to this session."
    }


class _FakeStream:
    """Minimal async-iterable that yields encoded lines then EOF."""

    def __init__(self, data: bytes) -> None:
        self._lines = data.splitlines(keepends=True)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line
        raise StopAsyncIteration


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        data_out = stdout.encode("utf-8")
        data_err = stderr.encode("utf-8")
        self.stdout = _FakeStream(data_out)
        self.stderr = _FakeStream(data_err)
        self.killed = False

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True

    def close(self, *, kill_tree: bool = True) -> None:
        pass


def test_execute_prefers_meaningful_stdout_error_over_tty_warning() -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")
        stdout = """
[11:13:00] ERROR    Fatal error occurred
[11:13:00] ERROR    Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'You have reached your specified API usage limits. You will regain access on 2026-05-01 at 00:00 UTC.'}}
""".strip()
        stderr = "Warning: Input is not a terminal (fd=0)."

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=1, stdout=stdout, stderr=stderr)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            result = await bridge._execute("Investigate the voice system", mode="smart")

        assert result.success is False
        assert result.error is not None
        assert "API usage limits" in result.error
        assert "Input is not a terminal" not in result.error

    asyncio.run(_exercise())


def test_execute_does_not_report_tty_warning_when_stdout_is_empty() -> None:
    """Regression: subagent that exits 1 with empty stdout and an
    only-ignorable stderr (e.g. the prompt_toolkit non-TTY warning) used to
    surface that warning as the user-visible error. The fallback should
    return an actionable exit-code message instead."""

    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(
                returncode=1,
                stdout="",
                stderr="Warning: Input is not a terminal (fd=0).\n",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            result = await bridge._execute("Investigate the voice system", mode="smart")

        assert result.success is False
        assert result.error is not None
        assert "Input is not a terminal" not in result.error
        assert "Exit code 1" in result.error

    asyncio.run(_exercise())


def test_execute_passes_devnull_stdin_to_subprocess() -> None:
    """Defensive: the subagent should never inherit the parent's stdin.
    Explicit DEVNULL prevents prompt_toolkit's non-TTY warning and also
    guarantees the subagent can't block waiting on parent input."""

    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            await bridge._execute("Inspect recent voice changes", mode="smart")

        assert captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL

    asyncio.run(_exercise())


def test_execute_uses_legacy_env_override_for_smart_model() -> None:
    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("GPTME_VOICE_SUBAGENT_MODEL", "openai-subscription/gpt-5.4")
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            result = await bridge._execute("Inspect recent voice changes", mode="smart")

        assert result.success is True
        args = tuple(captured["args"])
        assert args[:4] == (
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{_SMART_MODE_SUBPROCESS_TIMEOUT_SECONDS}s",
        )
        assert args[4:11] == (
            "gptme",
            "--non-interactive",
            "--context",
            "files",
            "--model",
            "openai-subscription/gpt-5.4",
            "--tool-format",
        )
        assert args[11] == "tool"

    asyncio.run(_exercise())


def test_execute_uses_fast_model_override_without_touching_smart() -> None:
    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("GPTME_VOICE_SUBAGENT_MODEL_FAST", "openai/gpt-5-mini")
            mp.setenv("GPTME_VOICE_SUBAGENT_MODEL_SMART", "openai-subscription/gpt-5.4")
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            result = await bridge._execute("Inspect recent voice changes", mode="fast")

        assert result.success is True
        args = tuple(captured["args"])
        assert args[:4] == (
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{_FAST_MODE_SUBPROCESS_TIMEOUT_SECONDS}s",
        )
        assert "--model" in args
        model_index = args.index("--model") + 1
        assert args[model_index] == "openai/gpt-5-mini"

    asyncio.run(_exercise())


def test_execute_uses_smart_model_override_without_touching_fast() -> None:
    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("GPTME_VOICE_SUBAGENT_MODEL_FAST", "openai/gpt-5-mini")
            mp.setenv("GPTME_VOICE_SUBAGENT_MODEL_SMART", "openai-subscription/gpt-5.4")
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            result = await bridge._execute("Inspect recent voice changes", mode="smart")

        assert result.success is True
        args = tuple(captured["args"])
        assert args[:4] == (
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{_SMART_MODE_SUBPROCESS_TIMEOUT_SECONDS}s",
        )
        assert args[4:11] == (
            "gptme",
            "--non-interactive",
            "--context",
            "files",
            "--model",
            "openai-subscription/gpt-5.4",
            "--tool-format",
        )
        assert args[11] == "tool"

    asyncio.run(_exercise())


def test_execute_uses_env_override_for_gptme_path() -> None:
    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("GPTME_VOICE_SUBAGENT_PATH", "/fake/bin/gptme")
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            result = await bridge._execute("Inspect recent voice changes", mode="smart")

        assert result.success is True
        args = tuple(captured["args"])
        assert args[:5] == (
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{_SMART_MODE_SUBPROCESS_TIMEOUT_SECONDS}s",
            "/fake/bin/gptme",
        )

    asyncio.run(_exercise())


def test_execute_fast_mode_keeps_context_files() -> None:
    """fast mode should skip context_cmd without dropping project prompt files."""

    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            await bridge._execute("quick lookup", mode="fast")

        args = tuple(captured["args"])
        assert args[:4] == (
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{_FAST_MODE_SUBPROCESS_TIMEOUT_SECONDS}s",
        )
        assert "--context" in args
        assert "files" in args
        assert "--non-interactive" in args

    asyncio.run(_exercise())


def test_execute_smart_mode_keeps_context_loading() -> None:
    """smart mode must still pass --context files for full workspace awareness."""

    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            await bridge._execute("detailed analysis", mode="smart")

        args = tuple(captured["args"])
        assert args[:8] == (
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{_SMART_MODE_SUBPROCESS_TIMEOUT_SECONDS}s",
            "gptme",
            "--non-interactive",
            "--context",
            "files",
        )

    asyncio.run(_exercise())


def test_execute_fast_mode_uses_subprocess_timeout_wrapper() -> None:
    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            await bridge._execute("quick lookup", mode="fast")

        args = tuple(captured["args"])
        if _TIMEOUT_BINARY_AVAILABLE:
            assert args[:4] == (
                "timeout",
                "--signal=TERM",
                "--kill-after=5s",
                f"{_FAST_MODE_SUBPROCESS_TIMEOUT_SECONDS}s",
            )
        else:
            assert args[0] == bridge.gptme_path

    asyncio.run(_exercise())


def test_unset_env_timeout_fallbacks_keep_smart_at_previous_budget() -> None:
    """Unset-env fallbacks: fast 60+10, smart 120+10 (do not shrink smart)."""
    import os

    if os.environ.get("GPTME_VOICE_SUBAGENT_TIMEOUT_FAST_SECONDS") is None:
        assert _FAST_MODE_SUBPROCESS_TIMEOUT_SECONDS == 70
    if os.environ.get("GPTME_VOICE_SUBAGENT_TIMEOUT_SMART_SECONDS") is None:
        assert _SMART_MODE_SUBPROCESS_TIMEOUT_SECONDS == 130


def test_execute_smart_mode_uses_subprocess_timeout_wrapper() -> None:
    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            await bridge._execute("deep lookup", mode="smart")

        args = tuple(captured["args"])
        if _TIMEOUT_BINARY_AVAILABLE:
            assert args[:4] == (
                "timeout",
                "--signal=TERM",
                "--kill-after=5s",
                f"{_SMART_MODE_SUBPROCESS_TIMEOUT_SECONDS}s",
            )
        else:
            assert args[0] == bridge.gptme_path

    asyncio.run(_exercise())


def test_execute_passes_bounded_transcript_tail_to_wrapper() -> None:
    async def _exercise() -> None:
        captured: dict[str, object] = {}

        transcript = [
            {"role": "user", "text": "first question"},
            {"role": "assistant", "text": "first answer"},
            {"role": "user", "text": "second question"},
            {"role": "assistant", "text": "second answer"},
        ]

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            tail_file_index = args.index("--transcript-tail-file") + 1
            captured["tail_text"] = Path(args[tail_file_index]).read_text()
            turns_index = args.index("--transcript-tail-turns") + 1
            captured["tail_turns"] = args[turns_index]
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(
                workspace="/fake/workspace",
                transcript_provider=lambda: transcript,
            )
            bridge.transcript_tail_turns = 3
            bridge.transcript_tail_chars = 512
            result = await bridge._execute("detailed analysis", mode="smart")

        assert result.success is True
        args = tuple(captured["args"])
        assert "--transcript-tail-file" in args
        assert "--transcript-tail-turns" in args
        assert captured["tail_turns"] == "3"
        assert captured["tail_text"] == (
            "Assistant: first answer\nUser: second question\nAssistant: second answer"
        )

    asyncio.run(_exercise())


def test_execute_skips_transcript_tail_when_disabled() -> None:
    async def _exercise() -> None:
        captured: dict[str, object] = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(
                workspace="/fake/workspace",
                transcript_provider=lambda: [{"role": "user", "text": "hello"}],
            )
            bridge.transcript_tail_turns = 0
            result = await bridge._execute("detailed analysis", mode="smart")

        assert result.success is True
        args = tuple(captured["args"])
        assert "--transcript-tail-file" not in args
        assert "--transcript-tail-turns" not in args

    asyncio.run(_exercise())


def test_subagent_status_shows_last_output() -> None:
    """subagent_status should include last_output once the subagent produces output."""

    async def _exercise() -> None:
        class _SlowProcessWithOutput(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        output_written = asyncio.Event()

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            proc = _SlowProcessWithOutput(
                returncode=0,
                stdout="[INFO] Checking task status\n[INFO] Found 3 active tasks\n",
            )
            output_written.set()
            return proc

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "check active tasks", "mode": "fast"}
            )
            task_id = dispatch["task_id"]

            # Allow the stdout reader to consume all lines (needs multiple event loop turns)
            await output_written.wait()
            for _ in range(10):
                await asyncio.sleep(0)

            status = await bridge.handle_function_call("subagent_status", {})
            entry = next(
                (e for e in status["pending"] if e["task_id"] == task_id), None
            )
            assert entry is not None
            assert entry["stage"] == "running"
            assert entry["model"] == bridge.model_fast
            assert entry.get("last_output") is not None
            assert "Found 3 active tasks" in entry["last_output"]
            assert entry.get("timings") is not None
            assert entry["timings"]["spawn_to_first_output_seconds"] >= 0
            assert entry["timings"]["output_elapsed_seconds"] >= 0

            await bridge.handle_function_call("subagent_cancel", {"task_id": task_id})

    asyncio.run(_exercise())


def test_extract_error_text_prefers_meaningful_stderr() -> None:
    stdout = "some log output"
    stderr = "Traceback: real failure here"
    assert (
        GptmeToolBridge._extract_error_text(stdout, stderr, output="")
        == "Traceback: real failure here"
    )


def test_extract_error_text_filters_tty_warning_from_stderr() -> None:
    stderr = "Warning: Input is not a terminal (fd=0)."
    stdout = "[11:13:00] ERROR    API error: rate limited"
    error = GptmeToolBridge._extract_error_text(stdout, stderr, output="")
    assert "Input is not a terminal" not in error
    assert "API error" in error


def test_extract_error_text_scans_stdout_for_error_needles() -> None:
    stdout = "\n".join(
        [
            "INFO    starting",
            "ERROR   Error code: 429 too many requests",
            "INFO    shutting down",
        ]
    )
    error = GptmeToolBridge._extract_error_text(stdout, stderr="", output="")
    assert "Error code: 429" in error


def test_extract_error_text_returns_empty_when_nothing_useful() -> None:
    assert GptmeToolBridge._extract_error_text("", "", output="") == ""


def test_extract_error_text_falls_back_to_output_when_no_error_patterns() -> None:
    # No error patterns in stdout, but we have some output — return it
    stdout = "routine log line"
    output = "partial response"
    error = GptmeToolBridge._extract_error_text(stdout, stderr="", output=output)
    assert error == output


def test_execute_falls_back_to_stdout_when_response_file_empty(tmp_path) -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            # Subagent wrote nothing to the response file — mimic by letting
            # the bridge create an empty tempfile (default behaviour of
            # NamedTemporaryFile with delete=False) and returning stdout.
            return _FakeProcess(
                returncode=0, stdout="voice-friendly summary from stdout"
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            result = await bridge._execute("What is the weather?", mode="smart")

        assert result.success is True
        assert "voice-friendly summary from stdout" in result.output

    asyncio.run(_exercise())


def test_execute_handles_missing_gptme_binary() -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(
            gptme_path="/does/not/exist/gptme", workspace="/fake/workspace"
        )

        async def _raise_file_not_found(*_args, **_kwargs):
            raise FileNotFoundError

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _raise_file_not_found)
            result = await bridge._execute("whatever", mode="smart")

        assert result.success is False
        assert result.error is not None
        assert "/does/not/exist/gptme" in result.error

    asyncio.run(_exercise())


def test_execute_reports_timeout() -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(timeout=1, workspace="/fake/workspace")

        class _HangingProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(10)
                return -1

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _HangingProcess(returncode=-1)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            result = await bridge._execute("long running task", mode="smart")

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()

    asyncio.run(_exercise())


def test_extract_error_text_filters_sigpipe_shell_noise() -> None:
    """A SIGPIPE (signal 13) from a shell command the subagent ran internally
    (e.g. `find ... | xargs grep ... | head`) is benign downstream-pipe-close
    noise and must not be surfaced as the subagent error."""
    stderr = "xargs: grep: terminated by signal 13"
    error = GptmeToolBridge._extract_error_text("", stderr, output="")
    assert "terminated by signal 13" not in error
    # With no other meaningful signal, fall through to the empty -> exit-code path.
    assert error == ""


def test_extract_error_text_filters_known_broken_pipe_noise() -> None:
    stderr = "sort: write failed: standard output: Broken pipe"
    stdout = "[11:13:00] ERROR    Error code: 429 too many requests"
    error = GptmeToolBridge._extract_error_text(stdout, stderr, output="")
    assert "Broken pipe" not in error
    assert "Error code: 429" in error


def test_extract_error_text_keeps_generic_broken_pipe_error() -> None:
    stderr = "ERROR: Broken pipe on database connection socket"
    error = GptmeToolBridge._extract_error_text("", stderr, output="")
    assert error == stderr


def test_execute_timeout_exit_code_reports_clear_message_not_shell_noise() -> None:
    """Regression for the 2026-05-26 live-call failure: the fast subagent was
    killed by the wrapper's `timeout` (exit 124) mid-exploration, and the last
    stderr line was a benign `xargs: grep: terminated by signal 13` SIGPIPE.
    The user-facing error used to be that misleading shell line. It should be a
    clear, actionable timeout message instead."""

    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(
                returncode=124,
                stdout="[12:55:30] thinking... searching the workspace",
                stderr="xargs: grep: terminated by signal 13",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            result = await bridge._execute(
                "Summarize the software factory in this workspace", mode="fast"
            )

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()
        assert "signal 13" not in result.error
        assert "124" not in result.error

    asyncio.run(_exercise())


def test_execute_sigterm_exit_code_reported_as_timeout() -> None:
    """143 = 128 + SIGTERM — the kill the wrapper sends on expiry — is also a
    timeout, not a generic crash."""

    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=143, stdout="partial work")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            result = await bridge._execute("long lookup", mode="fast")

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()

    asyncio.run(_exercise())


def test_execute_sigkill_exit_code_reports_killed_not_timeout() -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(
                returncode=137,
                stdout="partial work",
                stderr="sort: write failed: standard output: Broken pipe",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            result = await bridge._execute("long lookup", mode="fast")

        assert result.success is False
        assert result.error is not None
        assert "killed" in result.error.lower()
        assert "out-of-memory" in result.error.lower()
        assert "broken pipe" not in result.error.lower()

    asyncio.run(_exercise())


def test_handle_function_call_hangup_fires_callback_when_wired() -> None:
    """hangup triggers on_hangup callback with optional reason."""

    async def _exercise() -> None:
        captured: dict[str, object] = {"calls": []}

        async def _on_hangup(reason: str | None) -> None:
            captured["calls"].append(reason)  # type: ignore[attr-defined]

        bridge = GptmeToolBridge(workspace="/fake/workspace", on_hangup=_on_hangup)
        result = await bridge.handle_function_call(
            "hangup", {"reason": "caller said goodbye"}
        )

        assert result["status"] == "hanging_up"
        # Give the background task a tick to run
        await asyncio.sleep(0)
        assert captured["calls"] == ["caller said goodbye"]

    asyncio.run(_exercise())


def test_handle_function_call_hangup_without_reason() -> None:
    """hangup works when arguments is empty (reason is optional)."""

    async def _exercise() -> None:
        captured: dict[str, object] = {"calls": []}

        async def _on_hangup(reason: str | None) -> None:
            captured["calls"].append(reason)  # type: ignore[attr-defined]

        bridge = GptmeToolBridge(workspace="/fake/workspace", on_hangup=_on_hangup)
        result = await bridge.handle_function_call("hangup", {})

        assert result["status"] == "hanging_up"
        await asyncio.sleep(0)
        assert captured["calls"] == [None]

    asyncio.run(_exercise())


def test_handle_function_call_hangup_returns_not_supported_without_callback() -> None:
    """hangup returns not_supported when server has no on_hangup wired."""

    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")  # no on_hangup
        result = await bridge.handle_function_call("hangup", {})
        assert result["status"] == "not_supported"
        assert "message" in result

    asyncio.run(_exercise())


def test_handle_function_call_subagent_still_works_alongside_hangup() -> None:
    """Adding hangup support does not regress subagent dispatch."""

    async def _exercise() -> None:
        async def _on_hangup(reason: str | None) -> None:  # pragma: no cover
            return None

        bridge = GptmeToolBridge(workspace="/fake/workspace", on_hangup=_on_hangup)

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=0, stdout="ok")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            result = await bridge.handle_function_call(
                "subagent", {"task": "check something"}
            )

        assert result["status"] == "dispatched"
        assert "task_id" in result

    asyncio.run(_exercise())


def test_handle_function_call_unknown_name_returns_error() -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")
        result = await bridge.handle_function_call("not_a_tool", {})
        assert "error" in result

    asyncio.run(_exercise())


def test_hangup_tool_advertised_in_openai_session_config() -> None:
    """Ensure the hangup tool is present in the OpenAI session tools list
    so the model can actually discover and call it.
    """
    from gptme_voice.realtime.openai_client import OpenAIRealtimeClient

    client = OpenAIRealtimeClient(api_key="test-key")
    tools = client._build_session_params("test", client._build_tools())["tools"]
    names = {tool["name"] for tool in tools}
    assert "hangup" in names, "hangup tool must be advertised in session.update"
    assert "subagent" in names, "subagent tool must also still be advertised"
    assert (
        "subagent_status" in names
    ), "subagent_status tool must be advertised so the model can check pending tasks"
    assert (
        "subagent_cancel" in names
    ), "subagent_cancel tool must be advertised so the model can cancel pending tasks"


def test_subagent_status_empty_when_no_tasks() -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")
        result = await bridge.handle_function_call("subagent_status", {})
        assert result["status"] == "ok"
        assert result["pending_count"] == 0
        assert result["pending"] == []

    asyncio.run(_exercise())


def test_subagent_status_lists_pending_dispatch() -> None:
    """After dispatching, status should show the task with metadata."""

    async def _exercise() -> None:
        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "check one thing", "mode": "fast"}
            )
            assert dispatch["status"] == "dispatched"
            task_id = dispatch["task_id"]

            for _ in range(5):  # the shielded spawn resolves a few ticks later
                await asyncio.sleep(0)

            status = await bridge.handle_function_call("subagent_status", {})
            assert status["status"] == "ok"
            assert status["pending_count"] == 1
            entry = status["pending"][0]
            assert entry["task_id"] == task_id
            assert entry["task"] == "check one thing"
            assert entry["mode"] == "fast"
            assert entry["stage"] == "starting"
            assert entry["model"] == bridge.model_fast
            assert entry["elapsed_seconds"] >= 0
            assert entry.get("timings") is not None
            assert entry["timings"]["dispatch_to_spawn_seconds"] >= 0
            assert entry["timings"]["spawn_elapsed_seconds"] >= 0
            assert "spawn_to_first_output_seconds" not in entry["timings"]

            # Clean up the background task
            await bridge.handle_function_call("subagent_cancel", {"task_id": task_id})

    asyncio.run(_exercise())


def test_subagent_status_shows_recent_completion_on_success() -> None:
    """After a task completes, subagent_status should list it in recent_completions."""

    async def _exercise() -> None:
        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(
                returncode=0, stdout="Dashboard has 3 sub-dashboards.\n"
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "list dashboards", "mode": "fast"}
            )
            task_id = dispatch["task_id"]

            # Let the background task finish
            for _ in range(20):
                await asyncio.sleep(0)

            status = await bridge.handle_function_call("subagent_status", {})
            assert status["pending_count"] == 0
            recent = status["recent_completions"]
            assert len(recent) == 1
            entry = recent[0]
            assert entry["task_id"] == task_id
            assert entry["status"] == "success"
            assert entry["returncode"] == 0
            assert entry["elapsed_seconds"] is not None
            assert "list dashboards" in entry["task"]

    asyncio.run(_exercise())


def test_subagent_status_shows_recent_timeout() -> None:
    """A timed-out task (returncode=124) should appear as 'timed_out' in recent_completions."""

    async def _exercise() -> None:
        # Simulate a timeout: process exits immediately with rc=124 (UNIX timeout cmd)
        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=124, stdout="partial result\n")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "explore dashboard portal", "mode": "fast"}
            )
            task_id = dispatch["task_id"]

            # Let the background task finish
            for _ in range(20):
                await asyncio.sleep(0)

            status = await bridge.handle_function_call("subagent_status", {})
            assert status["pending_count"] == 0
            recent = status["recent_completions"]
            assert len(recent) == 1
            entry = recent[0]
            assert entry["task_id"] == task_id
            assert entry["status"] == "timed_out"
            assert entry["returncode"] == 124
            assert "explore dashboard portal" in entry["task"]

    asyncio.run(_exercise())


def test_subagent_status_shows_asyncio_timeout() -> None:
    """Python asyncio.TimeoutError path records 'timed_out', not 'error'.

    When asyncio.wait_for fires (Python-side timeout), on_completed is never
    called by the subprocess path, so pending.returncode stays None without the
    fix.  Verify the explicit on_completed(124, ...) call in the handler makes
    the status surface correctly.
    """

    async def _exercise() -> None:
        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(1000)
                return 0  # never reached

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            # Very short timeout so asyncio.wait_for fires immediately
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=0.01)

            dispatch = await bridge.handle_function_call(
                "subagent",
                {"task": "slow task that triggers asyncio timeout", "mode": "fast"},
            )
            task_id = dispatch["task_id"]

            # Let the background task run and hit the timeout
            for _ in range(50):
                await asyncio.sleep(0.001)

            status = await bridge.handle_function_call("subagent_status", {})
            assert status["pending_count"] == 0
            recent = status["recent_completions"]
            assert len(recent) == 1
            entry = recent[0]
            assert entry["task_id"] == task_id
            assert (
                entry["status"] == "timed_out"
            ), f"expected timed_out, got {entry['status']}"
            assert entry["returncode"] == 124

    asyncio.run(_exercise())


def test_subagent_dispatch_defaults_to_fast_mode() -> None:
    """Missing mode should still dispatch on the fast live-call path."""

    async def _exercise() -> None:
        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "check one thing"}
            )
            assert dispatch["status"] == "dispatched"
            task_id = dispatch["task_id"]

            await asyncio.sleep(0)

            status = await bridge.handle_function_call("subagent_status", {})
            assert status["status"] == "ok"
            assert status["pending_count"] == 1
            entry = status["pending"][0]
            assert entry["task_id"] == task_id
            assert entry["mode"] == "fast"
            assert entry["model"] == bridge.model_fast

            await bridge.handle_function_call("subagent_cancel", {"task_id": task_id})

    asyncio.run(_exercise())


def test_completed_subagent_logs_timing_summary(caplog) -> None:
    async def _exercise() -> None:
        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(
                returncode=0,
                stdout="[INFO] Starting lookup\n[INFO] Done\n",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            with caplog.at_level(logging.INFO):
                dispatch = await bridge.handle_function_call(
                    "subagent", {"task": "quick lookup", "mode": "fast"}
                )

                task_id = dispatch["task_id"]
                for _ in range(10):
                    await asyncio.sleep(0)

            assert any(
                f"Task {task_id} timings" in record.message for record in caplog.records
            )

    asyncio.run(_exercise())


def test_subagent_cancel_unknown_task_id_returns_not_found() -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")
        result = await bridge.handle_function_call(
            "subagent_cancel", {"task_id": "task-999"}
        )
        assert result["status"] == "not_found"
        assert "task-999" in result["message"]

    asyncio.run(_exercise())


def test_subagent_cancel_with_no_pending_returns_no_pending() -> None:
    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")
        result = await bridge.handle_function_call("subagent_cancel", {})
        assert result["status"] == "no_pending"

    asyncio.run(_exercise())


def test_subagent_cancel_specific_task_injects_cancel_notice() -> None:
    async def _exercise() -> None:
        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        injected: list[str] = []

        async def _on_result(text: str) -> None:
            injected.append(text)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(
                workspace="/fake/workspace", timeout=10, on_result=_on_result
            )

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "do a thing"}
            )
            task_id = dispatch["task_id"]
            await asyncio.sleep(0)

            result = await bridge.handle_function_call(
                "subagent_cancel", {"task_id": task_id}
            )
            assert result["status"] == "cancelled"
            assert result["task_id"] == task_id
            assert result["cancelled"] is True

            status = await bridge.handle_function_call("subagent_status", {})
            assert status["pending_count"] == 0

            assert injected, "expected on_result to be called with cancel notice"
            assert "cancelled" in injected[0].lower()

    asyncio.run(_exercise())


def test_execute_kills_process_on_cancel() -> None:
    """Cancelling a running _execute call must kill the underlying OS process."""

    async def _exercise() -> None:
        killed: list[bool] = []

        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(10)
                return 0

            def kill(self) -> None:
                killed.append(True)

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=30)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            task = asyncio.create_task(bridge._execute("long task", mode="fast"))
            await asyncio.sleep(0)  # let the task start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert killed, "process.kill() must be called when _execute is cancelled"

    asyncio.run(_exercise())


def test_get_timings_is_empty_before_any_dispatch() -> None:
    bridge = GptmeToolBridge(workspace="/fake/workspace")
    assert bridge.get_timings() == []


def test_get_timings_records_completed_subagent_run() -> None:
    async def _exercise() -> None:
        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(
                returncode=0,
                stdout="[INFO] quick lookup complete\n",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "check active tasks", "mode": "fast"}
            )
            task_id = dispatch["task_id"]

            # Let the background task run to completion.
            for _ in range(20):
                await asyncio.sleep(0)

        timings = bridge.get_timings()
        assert len(timings) == 1
        record = timings[0]
        assert record["task_id"] == task_id
        assert record["mode"] == "fast"
        assert record["model"] == bridge.model_fast
        assert record["returncode"] == 0
        assert "check active tasks" in record["task_preview"]
        assert "timings" in record
        assert record["timings"]["total_seconds"] >= 0

        # Returned records must be copies, not shared state.
        record["timings"]["total_seconds"] = 999.0
        assert bridge.get_timings()[0]["timings"]["total_seconds"] != 999.0

    asyncio.run(_exercise())


def test_subagent_cancel_all_cancels_every_pending_task() -> None:
    async def _exercise() -> None:
        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            await bridge.handle_function_call("subagent", {"task": "a"})
            await bridge.handle_function_call("subagent", {"task": "b"})
            await asyncio.sleep(0)

            result = await bridge.handle_function_call("subagent_cancel", {})
            assert result["status"] == "cancelled_all"
            assert result["cancelled_count"] == 2

            status = await bridge.handle_function_call("subagent_status", {})
            assert status["pending_count"] == 0

    asyncio.run(_exercise())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Reading /proc/<pid>/stat races with process exit: the entry can vanish
    # after the os.kill() check. Python 3.12 raises ProcessLookupError there,
    # older versions raise FileNotFoundError — either way the process is gone,
    # so a vanished /proc entry means dead. A reaped-but-unwaited child shows
    # up as a zombie; treat it as dead too.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.split(") ", 1)[1].split()[0] != "Z"


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_pid_alive_proc_entry_vanished(monkeypatch: pytest.MonkeyPatch, error) -> None:
    monkeypatch.setattr(os, "kill", lambda *_: None)
    monkeypatch.setattr(Path, "exists", lambda _: False)

    def vanished(_path):
        raise error()

    monkeypatch.setattr(Path, "read_text", vanished)
    assert not _pid_alive(12345)


def test_pid_alive_does_not_hide_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "kill", lambda *_: None)

    def denied(_path):
        raise PermissionError()

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        _pid_alive(12345)


@pytest.mark.parametrize("state, expected", [("S", True), ("Z", False)])
def test_pid_alive_proc_state(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: bool
) -> None:
    monkeypatch.setattr(os, "kill", lambda *_: None)
    monkeypatch.setattr(Path, "read_text", lambda _: f"12345 (sleep) {state} 1 2 3")
    assert _pid_alive(12345) is expected


@pytest.mark.skipif(os.name != "posix", reason="needs waitpid and process groups")
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_running_subagent_is_reaped_before_execute_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """A killed Popen child must not wait for a later spawn to be reaped."""
    script = tmp_path / "fake-subagent.sh"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    monkeypatch.delenv("GPTME_VOICE_SUBAGENT_PATH", raising=False)
    monkeypatch.setattr(tool_bridge, "_TIMEOUT_BINARY_AVAILABLE", False)
    real_spawn = tool_bridge._spawn_subagent
    processes: list[tool_bridge._SubagentProcess] = []

    async def _capture_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        # Retain Popen so its destructor cannot accidentally reap for the test.
        processes.append(process)
        return process

    monkeypatch.setattr(tool_bridge, "_spawn_subagent", _capture_spawn)

    async def _exercise() -> None:
        started = asyncio.Event()
        bridge = GptmeToolBridge(workspace=str(tmp_path), gptme_path=str(script))
        monkeypatch.setattr(tool_bridge, "_FAST_MODE_SUBPROCESS_TIMEOUT_SECONDS", 0.1)
        task = asyncio.create_task(
            bridge._execute(
                "long task", mode="fast", on_started=lambda _: started.set()
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, timeout=5)
            assert not result.success
            assert "timed out" in (result.error or "")
        # Unlike _pid_alive(), this distinguishes a zombie from a reaped child.
        with pytest.raises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)

    try:
        asyncio.run(_exercise())
    finally:
        for process in processes:
            tool_bridge._kill_process_tree(process)
            process.close()
            process._process.wait(timeout=1)


@pytest.mark.skipif(os.name != "posix", reason="needs waitpid and process groups")
@pytest.mark.parametrize("output_stream", ["stdout", "stderr"])
@pytest.mark.parametrize("leader_exits", [False, True])
def test_subagent_reader_error_delivers_result_and_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_stream: str,
    leader_exits: bool,
) -> None:
    """A line-limit error must not be replaced by cleanup's TimeoutExpired."""
    import sys

    pid_file = tmp_path / "descendant.pid"
    fork_child = (
        "if os.fork():\n"
        "    os._exit(0)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        if leader_exits
        else ""
    )
    script = tmp_path / "fake-subagent.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        + fork_child
        + f"sys.{output_stream}.write('x' * 70_000 + '\\n')\n"
        f"sys.{output_stream}.flush()\n"
        "time.sleep(30)\n"
    )
    script.chmod(0o755)
    monkeypatch.delenv("GPTME_VOICE_SUBAGENT_PATH", raising=False)
    monkeypatch.setattr(tool_bridge, "_TIMEOUT_BINARY_AVAILABLE", False)
    real_spawn = tool_bridge._spawn_subagent
    processes: list[tool_bridge._SubagentProcess] = []
    temporary_files: list[Path] = []

    async def _capture_spawn(*args, **kwargs):
        temporary_files.extend(tmp_path.glob("gptme-voice-*"))
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        if leader_exits:
            # Guarantee cleanup sees an exited leader, not a scheduling race
            # where the reader fails before the wrapper has finished exiting.
            await asyncio.wait_for(process.wait(), timeout=5)
        return process

    monkeypatch.setattr(tool_bridge, "_spawn_subagent", _capture_spawn)
    monkeypatch.setattr(tool_bridge.tempfile, "tempdir", str(tmp_path))

    async def _exercise() -> None:
        on_result = AsyncMock()
        bridge = GptmeToolBridge(
            workspace=str(tmp_path),
            gptme_path=str(script),
            on_result=on_result,
            transcript_provider=lambda: [{"role": "user", "text": "Test cleanup"}],
        )
        bridge.transcript_tail_turns = 1
        bridge.transcript_tail_chars = 1_600
        dispatch = await bridge.handle_function_call("subagent", {"task": "read"})
        task_id = dispatch["task_id"]
        pending = bridge._pending_tasks[task_id]
        await asyncio.wait_for(pending.task, timeout=5)
        on_result.assert_awaited_once()
        assert "longer than limit" in on_result.call_args.args[0]
        status = await bridge.handle_function_call("subagent_status", {})
        assert status["pending"] == []
        assert status["recent_completions"][0]["status"] == "error"
        assert len(temporary_files) == 2
        assert all(not path.exists() for path in temporary_files)
        with pytest.raises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)

    try:
        asyncio.run(_exercise())
        if leader_exits:
            descendant = int(pid_file.read_text())
            for _ in range(40):
                if not _pid_alive(descendant):
                    break
                time.sleep(0.05)
            assert not _pid_alive(descendant), "reader error left a live descendant"
    finally:
        for process in processes:
            tool_bridge._kill_process_tree(process)
            process.close()


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
def test_successful_execute_does_not_kill_same_group_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful tool call must not SIGKILL leftover same-group children.

    ``close()`` runs in ``_execute``'s finally after a successful return.
    Killpg belongs to cancel/timeout/error; success only reaps the wrapper.
    The descendant redirects stdio so the wrapper's pipes can EOF.
    """
    import sys

    pid_file = tmp_path / "descendant.pid"
    script = tmp_path / "fake-subagent.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        "from pathlib import Path\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    devnull = os.open(os.devnull, os.O_RDWR)\n"
        "    os.dup2(devnull, 0)\n"
        "    os.dup2(devnull, 1)\n"
        "    os.dup2(devnull, 2)\n"
        f"    Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "for _ in range(50):\n"
        f"    if Path({str(pid_file)!r}).exists():\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "raise SystemExit(0)\n"
    )
    script.chmod(0o755)
    monkeypatch.delenv("GPTME_VOICE_SUBAGENT_PATH", raising=False)
    monkeypatch.setattr(tool_bridge, "_TIMEOUT_BINARY_AVAILABLE", False)
    descendant: int | None = None

    async def _exercise() -> None:
        nonlocal descendant
        bridge = GptmeToolBridge(workspace=str(tmp_path), gptme_path=str(script))
        result = await asyncio.wait_for(
            bridge._execute("start a background helper", mode="fast"),
            timeout=5,
        )
        assert result.success
        descendant = int(pid_file.read_text())
        assert _pid_alive(
            descendant
        ), "success path killed an intentional same-group background child"

    try:
        asyncio.run(_exercise())
    finally:
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")
def test_cancel_kills_subagent_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel must kill the whole tree, not just the direct ``timeout`` child.

    Regression for the 2026-10-16 call: the lookup was cancelled at 27 s but the
    wrapper's gptme kept running (and logging) afterwards.
    """
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "fake-subagent.sh"
    script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n")
    script.chmod(0o755)
    monkeypatch.delenv("GPTME_VOICE_SUBAGENT_PATH", raising=False)

    async def _exercise() -> int:
        bridge = GptmeToolBridge(
            workspace=str(tmp_path), gptme_path=str(script), timeout=30
        )
        task = asyncio.create_task(bridge._execute("long task", mode="fast"))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        grandchild = int(pid_file.read_text().strip())
        assert _pid_alive(grandchild)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return grandchild

    grandchild = asyncio.run(_exercise())
    for _ in range(40):
        if not _pid_alive(grandchild):
            break
        time.sleep(0.05)
    assert not _pid_alive(grandchild), "cancelled subagent left a live descendant"


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")
@pytest.mark.parametrize("pipe_number", [1, 2])
def test_loop_shutdown_during_spawn_kills_subagent_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pipe_number: int
) -> None:
    """Shutdown cancels every task, not just the caller awaiting the spawn."""
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "fake-subagent.sh"
    script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n")
    script.chmod(0o755)
    monkeypatch.delenv("GPTME_VOICE_SUBAGENT_PATH", raising=False)
    monkeypatch.setattr(
        "gptme_voice.realtime.tool_bridge._TIMEOUT_BINARY_AVAILABLE", False
    )

    async def _exercise() -> int:
        spawned = asyncio.Event()
        loop = asyncio.get_running_loop()
        real_connect = loop.connect_read_pipe
        connections = 0

        async def _slow_connect(*args, **kwargs):
            nonlocal connections
            connections += 1
            if connections == pipe_number:
                while not pid_file.exists() or not pid_file.read_text().strip():
                    await asyncio.sleep(0.01)
                spawned.set()
                await asyncio.sleep(0.2)
            return await real_connect(*args, **kwargs)

        monkeypatch.setattr(loop, "connect_read_pipe", _slow_connect)
        bridge = GptmeToolBridge(
            workspace=str(tmp_path), gptme_path=str(script), timeout=30
        )
        asyncio.create_task(bridge._execute("long task", mode="fast"))
        await asyncio.wait_for(spawned.wait(), timeout=10)
        # No explicit cancel/await: asyncio.run() itself cancels ALL pending tasks.
        return int(pid_file.read_text().strip())

    grandchild = asyncio.run(_exercise())
    try:
        for _ in range(40):
            if not _pid_alive(grandchild):
                break
            time.sleep(0.01)
        assert not _pid_alive(grandchild), "loop shutdown left a live descendant"
    finally:
        if _pid_alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")
@pytest.mark.parametrize("repeat_cancel", [False, True])
@pytest.mark.parametrize("leader_exits", [False, True])
def test_cancel_during_spawn_kills_subagent_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repeat_cancel: bool,
    leader_exits: bool,
) -> None:
    """A cancel while the spawn is still connecting pipes must kill the tree.

    CPython's subprocess transport answers a cancel in that window with
    ``transp.close()``, which kills only the direct child. The fake below
    models that: it spawns the real tree, holds the await open, and on cancel
    kills only the direct child, exactly like the stdlib path.
    """
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "fake-subagent.sh"
    script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n")
    script.chmod(0o755)
    monkeypatch.delenv("GPTME_VOICE_SUBAGENT_PATH", raising=False)
    if leader_exits:
        script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\n")
        monkeypatch.setattr(
            "gptme_voice.realtime.tool_bridge._TIMEOUT_BINARY_AVAILABLE", False
        )
    real_exec = tool_bridge._spawn_subagent

    async def _exercise() -> int:
        spawned = asyncio.Event()

        async def _slow_connect(*args, **kwargs):
            proc = await real_exec(*args, **kwargs)
            try:
                for _ in range(100):
                    if pid_file.exists() and pid_file.read_text().strip():
                        break
                    await asyncio.sleep(0.05)
                if leader_exits:
                    for _ in range(100):
                        if proc.returncode is not None:
                            break
                        await asyncio.sleep(0.01)
                    assert proc.returncode == 0
                spawned.set()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                proc.kill()  # what transp.close() does
                raise
            return proc

        monkeypatch.setattr(tool_bridge, "_spawn_subagent", _slow_connect)
        bridge = GptmeToolBridge(
            workspace=str(tmp_path), gptme_path=str(script), timeout=30
        )
        task = asyncio.create_task(bridge._execute("long task", mode="fast"))
        await asyncio.wait_for(spawned.wait(), timeout=10)
        grandchild = int(pid_file.read_text().strip())
        assert _pid_alive(grandchild)
        task.cancel()
        if repeat_cancel:
            asyncio.get_running_loop().call_later(0.1, task.cancel)
        done, _ = await asyncio.wait({task}, timeout=2)
        if not done:
            # Unblock inherited pipes before reporting a cleanup hang.
            os.kill(grandchild, signal.SIGKILL)
            with pytest.raises(asyncio.CancelledError):
                await task
            pytest.fail("cancellation cleanup waited for a surviving descendant")
        with pytest.raises(asyncio.CancelledError):
            await task
        # Return immediately: asyncio.run() must be safe to shut down now.
        return grandchild

    grandchild = asyncio.run(_exercise())
    try:
        for _ in range(40):
            if not _pid_alive(grandchild):
                break
            time.sleep(0.05)
        assert not _pid_alive(grandchild), "cancel during spawn left a live descendant"
    finally:
        # A failed regression must not leave the test's real sleep running.
        if _pid_alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")
@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_cancel_during_spawn_bounds_escaped_descendant_pipe_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repeat_cancel: bool
) -> None:
    """An escaped process retaining the pipes must not block loop shutdown."""
    pid_file = tmp_path / "escaped.pid"
    script = tmp_path / "fake-subagent.sh"
    script.write_text(
        "#!/bin/sh\n" f"setsid sh -c 'echo $$ > {pid_file}; exec sleep 30' &\nwait\n"
    )
    script.chmod(0o755)
    monkeypatch.delenv("GPTME_VOICE_SUBAGENT_PATH", raising=False)
    monkeypatch.setattr(
        "gptme_voice.realtime.tool_bridge._TIMEOUT_BINARY_AVAILABLE", False
    )
    real_exec = tool_bridge._spawn_subagent
    escaped: int | None = None

    async def _exercise() -> None:
        nonlocal escaped
        spawned = asyncio.Event()
        processes = []

        async def _slow_connect(*args, **kwargs):
            proc = await real_exec(*args, **kwargs)
            processes.append(proc)
            for _ in range(100):
                if pid_file.exists() and pid_file.read_text().strip():
                    break
                await asyncio.sleep(0.01)
            spawned.set()
            await asyncio.sleep(0.1)
            return proc

        monkeypatch.setattr(tool_bridge, "_spawn_subagent", _slow_connect)
        bridge = GptmeToolBridge(
            workspace=str(tmp_path), gptme_path=str(script), timeout=30
        )
        task = asyncio.create_task(bridge._execute("long task", mode="fast"))
        await asyncio.wait_for(spawned.wait(), timeout=10)
        escaped = int(pid_file.read_text().strip())
        assert _pid_alive(escaped)
        assert os.getpgid(escaped) != processes[0].pid
        task.cancel()
        if repeat_cancel:
            asyncio.get_running_loop().call_later(0.2, task.cancel)
        done, _ = await asyncio.wait({task}, timeout=3)
        if not done:
            # Release inherited pipes so a failing test can still shut down.
            os.kill(escaped, signal.SIGKILL)
            with pytest.raises(asyncio.CancelledError):
                await task
            pytest.fail("cancellation hung on escaped descendant's pipes")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        # Do not kill the escaped process until AFTER asyncio.run() shuts down.
        # This tests transport closure, not a claim of detached-process containment.

    try:
        asyncio.run(_exercise())
    finally:
        if escaped is not None and _pid_alive(escaped):
            os.kill(escaped, signal.SIGKILL)


def test_cancelled_task_records_timing_receipt_and_completion() -> None:
    """A cancelled lookup must leave evidence for the call archive and status."""

    async def _exercise() -> None:
        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)
            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "check issue 1287", "mode": "fast"}
            )
            task_id = dispatch["task_id"]
            await asyncio.sleep(0)
            await bridge.handle_function_call("subagent_cancel", {"task_id": task_id})
            status = await bridge.handle_function_call("subagent_status", {})

        timings = bridge.get_timings()
        assert len(timings) == 1
        assert timings[0]["task_id"] == task_id
        assert timings[0]["cancelled"] is True
        assert timings[0]["returncode"] != 0  # never counts as a successful lookup
        assert "total_seconds" in timings[0]["timings"]

        recent = status["recent_completions"]
        assert [r["status"] for r in recent] == ["cancelled"]
        assert recent[0]["task_id"] == task_id

    asyncio.run(_exercise())


def test_subagent_status_reports_remaining_budget() -> None:
    async def _exercise() -> None:
        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)
            await bridge.handle_function_call("subagent", {"task": "a", "mode": "fast"})
            await asyncio.sleep(0)
            status = await bridge.handle_function_call("subagent_status", {})
            await bridge.handle_function_call("subagent_cancel", {})

        pending = status["pending"][0]
        assert pending["budget_seconds"] == _FAST_MODE_SUBPROCESS_TIMEOUT_SECONDS - 10
        assert 0 < pending["remaining_seconds"] <= pending["budget_seconds"]

    asyncio.run(_exercise())


# --- handoff_to_agent tests ---


def test_handoff_not_supported_when_callback_is_none() -> None:
    """Without on_handoff wired, handoff_to_agent returns not_supported."""

    async def _exercise() -> None:
        bridge = GptmeToolBridge(workspace="/fake/workspace")
        result = await bridge.handle_function_call(
            "handoff_to_agent", {"to_agent": "alice", "reason": "caller asked"}
        )
        assert result["status"] == "not_supported"
        assert "GPTME_VOICE_HANDOFF_DIR" in result["message"]

    asyncio.run(_exercise())


def test_handoff_invokes_callback_with_correct_args() -> None:
    """When on_handoff is wired, the callback receives to_agent, reason, context_summary."""

    async def _exercise() -> None:
        captured: dict = {}

        async def _fake_handoff(
            to_agent: str, reason: str, context_summary: str | None
        ) -> dict:
            captured["to_agent"] = to_agent
            captured["reason"] = reason
            captured["context_summary"] = context_summary
            return {
                "status": "handoff_initiated",
                "handoff_id": "test-id",
                "to_agent": to_agent,
                "message": "ok",
            }

        bridge = GptmeToolBridge(
            workspace="/fake/workspace",
            on_handoff=_fake_handoff,
        )
        result = await bridge.handle_function_call(
            "handoff_to_agent",
            {
                "to_agent": "alice",
                "reason": "caller asked for Alice",
                "context_summary": "discussing gptme issues",
            },
        )
        assert result["status"] == "handoff_initiated"
        assert result["to_agent"] == "alice"
        assert captured["to_agent"] == "alice"
        assert captured["reason"] == "caller asked for Alice"
        assert captured["context_summary"] == "discussing gptme issues"

    asyncio.run(_exercise())


def test_handoff_passes_none_context_summary_when_absent() -> None:
    """context_summary is optional; when absent the callback gets None."""

    async def _exercise() -> None:
        captured: dict = {}

        async def _fake_handoff(
            to_agent: str, reason: str, context_summary: str | None
        ) -> dict:
            captured["context_summary"] = context_summary
            return {
                "status": "handoff_initiated",
                "to_agent": to_agent,
                "message": "ok",
            }

        bridge = GptmeToolBridge(workspace="/fake/workspace", on_handoff=_fake_handoff)
        await bridge.handle_function_call(
            "handoff_to_agent", {"to_agent": "gordon", "reason": "financial question"}
        )
        assert captured["context_summary"] is None

    asyncio.run(_exercise())


def test_on_dispatch_called_when_subagent_dispatched() -> None:
    """on_dispatch callback fires immediately after a subagent task is enqueued."""

    async def _exercise() -> None:
        dispatch_calls: list[None] = []

        async def _fake_on_dispatch() -> None:
            dispatch_calls.append(None)

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(
                workspace="/fake/workspace",
                on_dispatch=_fake_on_dispatch,
            )

            result = await bridge.handle_function_call(
                "subagent", {"task": "list recent commits", "mode": "fast"}
            )

        assert result["status"] == "dispatched"
        for _ in range(20):
            if dispatch_calls:
                break
            await asyncio.sleep(0)
        assert len(dispatch_calls) == 1

    asyncio.run(_exercise())


def test_on_dispatch_failure_does_not_hide_dispatch_receipt(caplog) -> None:
    """A failed cue send must not orphan an already-running subagent."""

    async def _exercise() -> None:
        async def _failing_on_dispatch() -> None:
            raise ConnectionError("voice transport closed")

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(
                workspace="/fake/workspace",
                on_dispatch=_failing_on_dispatch,
            )

            with caplog.at_level(logging.ERROR):
                result = await bridge.handle_function_call(
                    "subagent", {"task": "list recent commits", "mode": "fast"}
                )

            assert result["status"] == "dispatched"
            assert result["task_id"] in bridge._pending_tasks

            for _ in range(20):
                await asyncio.sleep(0)
            assert "Failed to send subagent dispatch cue" in caplog.text

    asyncio.run(_exercise())


def test_on_dispatch_timeout_does_not_hide_dispatch_receipt(
    caplog, monkeypatch
) -> None:
    """A stalled cue send must not orphan an already-running subagent."""

    async def _exercise() -> None:
        cue_cancelled = asyncio.Event()

        async def _stalled_on_dispatch() -> None:
            try:
                await asyncio.Future()
            finally:
                cue_cancelled.set()

        class _SlowProcess(_FakeProcess):
            async def wait(self) -> int:
                await asyncio.sleep(5)
                return 0

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _SlowProcess(returncode=0)

        monkeypatch.setattr(
            "gptme_voice.realtime.tool_bridge._CUE_CALLBACK_TIMEOUT_SECONDS", 0.01
        )
        monkeypatch.setattr(
            tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec
        )
        bridge = GptmeToolBridge(
            workspace="/fake/workspace",
            on_dispatch=_stalled_on_dispatch,
        )

        with caplog.at_level(logging.WARNING):
            result = await bridge.handle_function_call(
                "subagent", {"task": "list recent commits", "mode": "fast"}
            )

        assert result["status"] == "dispatched"
        assert result["task_id"] in bridge._pending_tasks
        assert not cue_cancelled.is_set()

        for _ in range(20):
            await asyncio.sleep(0.001)
        assert cue_cancelled.is_set()
        assert "Timed out sending subagent dispatch cue" in caplog.text

        await bridge.handle_function_call(
            "subagent_cancel", {"task_id": result["task_id"]}
        )

    asyncio.run(_exercise())


def test_on_dispatch_not_called_for_non_subagent_calls() -> None:
    """on_dispatch must not fire for subagent_status or other tool calls."""

    async def _exercise() -> None:
        dispatch_calls: list[None] = []

        async def _fake_on_dispatch() -> None:
            dispatch_calls.append(None)

        bridge = GptmeToolBridge(
            workspace="/fake/workspace",
            on_dispatch=_fake_on_dispatch,
        )
        await bridge.handle_function_call("subagent_status", {})
        assert len(dispatch_calls) == 0

    asyncio.run(_exercise())


def test_on_timeout_called_when_subagent_times_out() -> None:
    """on_timeout fires when the subagent process exits with a timeout returncode."""

    async def _exercise() -> None:
        timeout_calls: list[None] = []
        result_calls: list[str] = []

        async def _fake_on_timeout() -> None:
            timeout_calls.append(None)

        async def _fake_on_result(text: str) -> None:
            result_calls.append(text)

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=124, stdout="partial output\n")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(
                workspace="/fake/workspace",
                timeout=10,
                on_result=_fake_on_result,
                on_timeout=_fake_on_timeout,
            )

            await bridge.handle_function_call(
                "subagent", {"task": "slow analysis task", "mode": "fast"}
            )

            # Drain the event loop until the background task completes
            for _ in range(20):
                await asyncio.sleep(0)

        assert len(timeout_calls) == 1, "on_timeout should fire exactly once"
        assert len(result_calls) == 1, "on_result still fires after timeout"

    asyncio.run(_exercise())


def test_on_timeout_failure_does_not_suppress_result_or_cleanup(caplog) -> None:
    """A failed timeout cue must not interrupt task completion."""

    async def _exercise() -> None:
        result_calls: list[str] = []

        async def _failing_on_timeout() -> None:
            raise ConnectionError("voice transport closed")

        async def _fake_on_result(text: str) -> None:
            result_calls.append(text)

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=124, stdout="partial output\n")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(
                workspace="/fake/workspace",
                timeout=10,
                on_result=_fake_on_result,
                on_timeout=_failing_on_timeout,
            )

            with caplog.at_level(logging.ERROR):
                dispatch = await bridge.handle_function_call(
                    "subagent", {"task": "slow analysis task", "mode": "fast"}
                )
                for _ in range(20):
                    await asyncio.sleep(0)

            assert len(result_calls) == 1
            assert dispatch["task_id"] not in bridge._pending_tasks
            assert "Failed to send subagent timeout cue" in caplog.text

    asyncio.run(_exercise())


def test_on_timeout_callback_timeout_does_not_suppress_result_or_cleanup(
    caplog, monkeypatch
) -> None:
    """A stalled timeout cue must not interrupt task completion."""

    async def _exercise() -> None:
        result_calls: list[str] = []
        cue_cancelled = asyncio.Event()

        async def _stalled_on_timeout() -> None:
            try:
                await asyncio.Future()
            finally:
                cue_cancelled.set()

        async def _fake_on_result(text: str) -> None:
            result_calls.append(text)

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=124, stdout="partial output\n")

        monkeypatch.setattr(
            "gptme_voice.realtime.tool_bridge._CUE_CALLBACK_TIMEOUT_SECONDS", 0.01
        )
        monkeypatch.setattr(
            tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec
        )
        bridge = GptmeToolBridge(
            workspace="/fake/workspace",
            timeout=10,
            on_result=_fake_on_result,
            on_timeout=_stalled_on_timeout,
        )

        with caplog.at_level(logging.WARNING):
            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "slow analysis task", "mode": "fast"}
            )
            for _ in range(30):
                await asyncio.sleep(0.001)

        assert len(result_calls) == 1
        assert dispatch["task_id"] not in bridge._pending_tasks
        assert cue_cancelled.is_set()
        assert "Timed out sending subagent timeout cue" in caplog.text

    asyncio.run(_exercise())


def test_on_timeout_not_called_on_successful_completion() -> None:
    """on_timeout must not fire when the subagent exits successfully."""

    async def _exercise() -> None:
        timeout_calls: list[None] = []

        async def _fake_on_timeout() -> None:
            timeout_calls.append(None)

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProcess(returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tool_bridge, "_spawn_subagent", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(
                workspace="/fake/workspace",
                on_timeout=_fake_on_timeout,
            )

            await bridge.handle_function_call(
                "subagent", {"task": "quick lookup", "mode": "fast"}
            )

            for _ in range(20):
                await asyncio.sleep(0)

        assert len(timeout_calls) == 0

    asyncio.run(_exercise())
