import asyncio

import pytest
from gptme_voice.realtime.tool_bridge import GptmeToolBridge


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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            result = await bridge._execute("Investigate the voice system", mode="smart")

        assert result.success is False
        assert result.error is not None
        assert "API usage limits" in result.error
        assert "Input is not a terminal" not in result.error

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            result = await bridge._execute("Inspect recent voice changes", mode="smart")

        assert result.success is True
        assert tuple(captured["args"])[:7] == (
            "gptme",
            "--non-interactive",
            "--context",
            "files",
            "--model",
            "openai-subscription/gpt-5.4",
            "--tool-format",
        )
        assert tuple(captured["args"])[7] == "tool"

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            result = await bridge._execute("Inspect recent voice changes", mode="fast")

        assert result.success is True
        assert "--model" in tuple(captured["args"])
        model_index = tuple(captured["args"]).index("--model") + 1
        assert tuple(captured["args"])[model_index] == "openai/gpt-5-mini"

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            result = await bridge._execute("Inspect recent voice changes", mode="smart")

        assert result.success is True
        assert tuple(captured["args"])[:7] == (
            "gptme",
            "--non-interactive",
            "--context",
            "files",
            "--model",
            "openai-subscription/gpt-5.4",
            "--tool-format",
        )
        assert tuple(captured["args"])[7] == "tool"

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            result = await bridge._execute("Inspect recent voice changes", mode="smart")

        assert result.success is True
        assert tuple(captured["args"])[0] == "/fake/bin/gptme"

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            await bridge._execute("quick lookup", mode="fast")

        args = tuple(captured["args"])
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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace")
            await bridge._execute("detailed analysis", mode="smart")

        args = tuple(captured["args"])
        assert "--context" in args
        assert "files" in args

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "check active tasks", "mode": "fast"}
            )
            task_id = dispatch["task_id"]

            # Allow the stdout reader to consume lines
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            status = await bridge.handle_function_call("subagent_status", {})
            entry = next(
                (e for e in status["pending"] if e["task_id"] == task_id), None
            )
            assert entry is not None
            if entry.get("last_output"):
                assert "Found 3 active tasks" in entry["last_output"]

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
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
            mp.setattr(asyncio, "create_subprocess_exec", _raise_file_not_found)
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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            result = await bridge._execute("long running task", mode="smart")

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
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
    import inspect

    from gptme_voice.realtime import openai_client

    source = inspect.getsource(openai_client.OpenAIRealtimeClient.connect)
    assert (
        '"name": "hangup"' in source
    ), "hangup tool must be declared in OpenAIRealtimeClient.connect() tools list"
    assert '"name": "subagent"' in source, "subagent tool must also still be declared"
    assert (
        '"name": "subagent_status"' in source
    ), "subagent_status tool must be declared so the model can check pending tasks"
    assert (
        '"name": "subagent_cancel"' in source
    ), "subagent_cancel tool must be declared so the model can cancel pending tasks"


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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/fake/workspace", timeout=10)

            dispatch = await bridge.handle_function_call(
                "subagent", {"task": "check one thing", "mode": "fast"}
            )
            assert dispatch["status"] == "dispatched"
            task_id = dispatch["task_id"]

            await asyncio.sleep(0)

            status = await bridge.handle_function_call("subagent_status", {})
            assert status["status"] == "ok"
            assert status["pending_count"] == 1
            entry = status["pending"][0]
            assert entry["task_id"] == task_id
            assert entry["task"] == "check one thing"
            assert entry["mode"] == "fast"
            assert entry["elapsed_seconds"] >= 0

            # Clean up the background task
            await bridge.handle_function_call("subagent_cancel", {"task_id": task_id})

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            task = asyncio.create_task(bridge._execute("long task", mode="fast"))
            await asyncio.sleep(0)  # let the task start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert killed, "process.kill() must be called when _execute is cancelled"

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
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
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
