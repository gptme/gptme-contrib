import asyncio
import json
from pathlib import Path

import pytest
from gptme_voice.realtime.openai_client import (
    OpenAIRealtimeClient,
    SessionConfig,
    _load_project_instructions,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


def test_connect_exposes_subagent_tool_as_focused_lookup_only() -> None:
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(
                api_key="test-key",
                session_config=SessionConfig(instructions="You are Bob."),
            )
            await client.connect()
            await asyncio.sleep(0)
            await client.disconnect()

        session_update = fake_ws.sent[0]
        tool = session_update["session"]["tools"][0]
        description = tool["description"]

        assert session_update["type"] == "session.update"
        assert tool["name"] == "subagent"
        assert "small, focused workspace lookup or action" in description
        assert "broad investigations" in description
        assert "post-call analysis" in description
        assert fake_ws.closed is True

    asyncio.run(_exercise())


def test_load_project_instructions_includes_post_call_follow_up_guard(
    tmp_path: Path,
) -> None:
    """Preamble must tell the live-call model not to claim post-call dispatch itself."""
    (tmp_path / "gptme.toml").write_text(
        '[prompt]\nfiles = ["ABOUT.md"]\n',
    )
    (tmp_path / "ABOUT.md").write_text("# ABOUT\nYou are Bob.\n")

    instructions = _load_project_instructions(str(tmp_path))

    # The preamble was applied (sanity — otherwise we'd get _DEFAULT_INSTRUCTIONS).
    assert "real-time voice conversation" in instructions

    # The new POST-CALL FOLLOW-UP section exists.
    assert "POST-CALL FOLLOW-UP:" in instructions

    # The core behavioral constraint from the 2026-04-20 call is present:
    # do not verbally claim to have dispatched post-call work during the call.
    assert "Do NOT claim, announce, or imply that you have dispatched" in instructions
    assert "post-call analysis dispatched" in instructions

    # Acknowledging automatic post-call follow-up is still allowed.
    assert "happen automatically after" in instructions


def test_load_project_instructions_guards_present_without_personality_files(
    tmp_path: Path,
) -> None:
    """Guards must apply even when a workspace has no matching personality files."""
    # Config exists but references a file that doesn't exist — no parts loaded.
    (tmp_path / "gptme.toml").write_text(
        '[prompt]\nfiles = ["MISSING.md"]\n',
    )

    instructions = _load_project_instructions(str(tmp_path))

    # Should NOT fall back to the bare _DEFAULT_INSTRUCTIONS.
    assert "You are a helpful assistant" not in instructions

    # Behavioral guards must still be present.
    assert "real-time voice conversation" in instructions
    assert "POST-CALL FOLLOW-UP:" in instructions
    assert "Do NOT claim, announce, or imply that you have dispatched" in instructions
