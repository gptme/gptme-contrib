import asyncio

import pytest
from gptme_voice.realtime.tool_bridge import GptmeToolBridge


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

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


def test_execute_uses_env_override_for_smart_model() -> None:
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
            async def communicate(self) -> tuple[bytes, bytes]:  # type: ignore[override]
                await asyncio.sleep(10)
                return b"", b""

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _HangingProcess(returncode=-1)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            result = await bridge._execute("long running task", mode="smart")

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error.lower()

    asyncio.run(_exercise())
