import asyncio
import base64
import json
import tempfile
from pathlib import Path

import pytest
from gptme_voice.realtime.server import (
    RecentCallRecord,
    TranscriptTurn,
    VoiceServer,
    _build_caller_instructions,
    _build_resume_instructions,
    _get_twilio_field,
)


class _DummyWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(message)


def test_build_caller_instructions_no_number() -> None:
    base = "You are Bob."
    result = _build_caller_instructions(base, "", None)
    assert result == base


def test_build_caller_instructions_unknown_number() -> None:
    result = _build_caller_instructions("You are Bob.", "+15551234567", None)
    assert "+15551234567" in result
    assert "unknown" in result.lower()
    assert "You are Bob." in result


def test_build_caller_instructions_known_number_from_people_dir() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        people_dir = Path(tmpdir) / "people"
        people_dir.mkdir()
        (people_dir / "erik-bjareholt.md").write_text(
            "# Erik Bjäreholt\n\nPhone: +46700000001\n"
        )
        result = _build_caller_instructions("You are Bob.", "+46700000001", tmpdir)
    assert "Erik Bjäreholt" in result
    assert "+46700000001" in result
    assert "You are Bob." in result


def test_get_twilio_field_prefers_camel_case() -> None:
    payload = {"streamSid": "MZ123", "stream_sid": "legacy"}

    assert _get_twilio_field(payload, "streamSid", "stream_sid") == "MZ123"


def test_get_twilio_field_falls_back_to_snake_case() -> None:
    payload = {"stream_sid": "legacy"}

    assert _get_twilio_field(payload, "streamSid", "stream_sid") == "legacy"


def test_send_to_twilio_uses_stream_sid_field_name() -> None:
    server = VoiceServer()
    websocket = _DummyWebSocket()

    asyncio.run(server._send_to_twilio(websocket, "MZ123", b"\x00\x01"))

    assert len(websocket.messages) == 1
    message = json.loads(websocket.messages[0])
    assert message == {
        "event": "media",
        "streamSid": "MZ123",
        "media": {"payload": base64.b64encode(b"\x00\x01").decode("utf-8")},
    }


def test_build_resume_instructions_includes_prior_transcript() -> None:
    record = RecentCallRecord(
        caller_id="+46700000001",
        source="twilio",
        ended_at=0,
        transcript=[
            TranscriptTurn(role="user", text="Hello Bob"),
            TranscriptTurn(role="assistant", text="Hi Erik"),
        ],
        metadata={},
    )

    result = _build_resume_instructions("You are Bob.", record, 300)

    assert "reconnected" in result
    assert "User: Hello Bob" in result
    assert "Assistant: Hi Erik" in result
    assert "You are Bob." in result


def test_recent_call_is_consumed_within_resume_window() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        server.resume_window_seconds = 300
        record = RecentCallRecord(
            caller_id="+46700000001",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="Hello again")],
            metadata={"from_number": "+46700000001"},
        )
        server._save_recent_call(record)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_100.0)
            resumed = server._consume_recent_call("+46700000001")

        assert resumed is not None
        assert resumed.caller_id == "+46700000001"
        assert resumed.transcript[0].text == "Hello again"


def test_recent_call_is_ignored_outside_resume_window() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        server.resume_window_seconds = 300
        record = RecentCallRecord(
            caller_id="+46700000001",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="Too old")],
            metadata={},
        )
        server._save_recent_call(record)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_400.1)
            resumed = server._consume_recent_call("+46700000001")

        assert resumed is None


def test_schedule_post_call_runs_configured_command_hook() -> None:
    async def _exercise() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = VoiceServer()
            server.state_dir = Path(tmpdir)
            server.post_call_command = "run-post-call"
            server.post_call_delay_seconds = 0
            record = RecentCallRecord(
                caller_id="+46700000001",
                source="twilio",
                ended_at=1_000.0,
                transcript=[TranscriptTurn(role="user", text="Follow up")],
                metadata={},
            )
            record_path = server._save_recent_call(record)
            observed: dict[str, str] = {}

            async def _fake_run_post_call(caller_id: str, path: Path) -> None:
                observed["caller_id"] = caller_id
                observed["path"] = str(path)

            server._run_post_call_command = _fake_run_post_call  # type: ignore[method-assign]

            await server._schedule_post_call(record.caller_id, record_path)
            task = server._pending_post_calls[record.caller_id]
            await task

            assert observed == {
                "caller_id": "+46700000001",
                "path": str(record_path),
            }

    asyncio.run(_exercise())


def test_consume_recent_call_deletes_state_file() -> None:
    """P2 fix: _consume_recent_call must remove the disk file so a crash-resume
    can't re-inject the old transcript on the next reconnect."""
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        server.resume_window_seconds = 300
        record = RecentCallRecord(
            caller_id="+46700000002",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="Delete me")],
            metadata={},
        )
        server._save_recent_call(record)
        state_path = server._recent_call_path("+46700000002")
        assert state_path.exists()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_100.0)
            server._consume_recent_call("+46700000002")

        assert not state_path.exists()


def test_schedule_post_call_runner_finally_does_not_evict_newer_task() -> None:
    """P1 fix: cancelling an old _runner task must not pop the newer task
    that replaced it in _pending_post_calls."""

    async def _exercise() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = VoiceServer()
            server.state_dir = Path(tmpdir)
            server.post_call_command = "run-post-call"
            server.post_call_delay_seconds = 1_000  # effectively never fires

            record = RecentCallRecord(
                caller_id="+46700000003",
                source="twilio",
                ended_at=1_000.0,
                transcript=[],
                metadata={},
            )
            record_path = server._save_recent_call(record)

            # Schedule first task (long sleep — won't complete naturally)
            await server._schedule_post_call(record.caller_id, record_path)
            first_task = server._pending_post_calls[record.caller_id]

            # Schedule second task — cancels first and registers itself
            await server._schedule_post_call(record.caller_id, record_path)
            second_task = server._pending_post_calls[record.caller_id]

            # Wait for the first task's finally-block to run
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            # The second task must still be registered
            assert server._pending_post_calls.get(record.caller_id) is second_task
            assert first_task.cancelled()

            second_task.cancel()

    asyncio.run(_exercise())
