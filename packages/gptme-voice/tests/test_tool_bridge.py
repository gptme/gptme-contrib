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
        bridge = GptmeToolBridge(workspace="/home/bob/bob")
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
            mp.setenv("GPTME_VOICE_SUBAGENT_MODEL_SMART", "openai-subscription/gpt-5.4")
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/home/bob/bob")
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
            mp.setenv("GPTME_VOICE_SUBAGENT_PATH", "/home/bob/.local/bin/gptme")
            mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
            bridge = GptmeToolBridge(workspace="/home/bob/bob")
            result = await bridge._execute("Inspect recent voice changes", mode="smart")

        assert result.success is True
        assert tuple(captured["args"])[0] == "/home/bob/.local/bin/gptme"

    asyncio.run(_exercise())
