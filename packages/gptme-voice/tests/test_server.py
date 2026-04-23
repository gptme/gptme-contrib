import asyncio
import base64
import json
import os
import shlex
import sys
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
    _truncate_resume_transcript,
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


def test_build_session_bootstrap_greets_fresh_calls() -> None:
    server = VoiceServer()
    server._instructions = "You are Bob."

    bootstrap = server._build_session_bootstrap(
        caller_id="+46700000011",
        from_number="+46700000011",
    )

    assert bootstrap.should_greet_first is True
    assert "+46700000011" in bootstrap.instructions
    assert "You are Bob." in bootstrap.instructions


def test_build_session_bootstrap_personalizes_known_caller_greeting() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        people_dir = Path(tmpdir) / "people"
        people_dir.mkdir()
        (people_dir / "erik-bjareholt.md").write_text(
            "# Erik Bjäreholt\n\nPhone: +46700000001\n"
        )
        server = VoiceServer(workspace=tmpdir)
        server._instructions = "You are Bob."

        bootstrap = server._build_session_bootstrap(
            caller_id="+46700000001",
            from_number="+46700000001",
        )

    assert bootstrap.should_greet_first is True
    assert "Erik Bjäreholt" in bootstrap.initial_response_instructions
    assert "Do NOT say 'thanks for calling'" in bootstrap.initial_response_instructions


def test_build_session_bootstrap_asks_unknown_caller_to_identify() -> None:
    server = VoiceServer()
    server._instructions = "You are Bob."

    bootstrap = server._build_session_bootstrap(
        caller_id="+15551234567",
        from_number="+15551234567",
    )

    assert bootstrap.should_greet_first is True
    assert "caller is unknown" in bootstrap.initial_response_instructions
    assert "Introduce yourself by name" in bootstrap.initial_response_instructions
    assert "Who am I speaking to?" in bootstrap.initial_response_instructions


def test_truncate_resume_transcript_keeps_line_boundaries() -> None:
    # Lines must exceed max_chars so truncation is actually triggered
    transcript_text = "\n".join(
        [
            f"User: {'a' * 1500}",
            f"Assistant: {'b' * 1500}",
            "User: tail",
        ]
    )

    truncated = _truncate_resume_transcript(transcript_text, 2_500)
    formatted_lines = transcript_text.splitlines()

    assert len(transcript_text) > 2_500, "input must exceed budget to test truncation"
    assert truncated.splitlines()[0] in formatted_lines
    assert truncated.endswith("User: tail")
    assert len(truncated) <= 2_500


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


def test_build_session_bootstrap_skips_greeting_for_recent_resume() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        server.resume_window_seconds = 300
        server._instructions = "You are Bob."
        record = RecentCallRecord(
            caller_id="+46700000012",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="Resume this call")],
            metadata={"from_number": "+46700000012"},
        )
        server._save_recent_call(record)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_100.0)
            bootstrap = server._build_session_bootstrap(caller_id=record.caller_id)

        assert bootstrap.should_greet_first is False
        assert "reconnected after a brief disconnect" in bootstrap.instructions
        assert "Resume this call" in bootstrap.instructions


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


def test_consume_handoff_bootstrap_returns_resume_context_and_deletes_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        server.resume_window_seconds = 300
        server._instructions = "You are Alice."
        bootstrap_path = server._handoff_bootstrap_path("handoff-123")
        bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_path.write_text(
            json.dumps(
                {
                    "protocol_version": 1,
                    "source": "voice_handoff",
                    "handoff_id": "handoff-123",
                    "accepted_at": "1970-01-01T00:18:20Z",
                    "resume_context": "bob transferred this caller to alice.",
                }
            )
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_200.0)
            instructions = server._build_session_instructions(
                caller_id="+46700000007",
                handoff_id="handoff-123",
            )

        assert "bob transferred this caller to alice." in instructions
        assert "You are Alice." in instructions
        assert not bootstrap_path.exists()


def test_stale_handoff_bootstrap_falls_back_to_recent_call_resume() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        server.resume_window_seconds = 300
        server._instructions = "You are Alice."
        record = RecentCallRecord(
            caller_id="+46700000008",
            source="twilio",
            ended_at=1_300.0,
            transcript=[TranscriptTurn(role="user", text="Resume the old call")],
            metadata={},
        )
        server._save_recent_call(record)

        bootstrap_path = server._handoff_bootstrap_path("handoff-stale")
        bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_path.write_text(
            json.dumps(
                {
                    "protocol_version": 1,
                    "source": "voice_handoff",
                    "handoff_id": "handoff-stale",
                    "accepted_at": "1970-01-01T00:16:40Z",
                    "resume_context": "stale handoff context",
                }
            )
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_600.0)
            instructions = server._build_session_instructions(
                caller_id=record.caller_id,
                handoff_id="handoff-stale",
            )

        assert "Resume the old call" in instructions
        assert "stale handoff context" not in instructions
        assert bootstrap_path.exists()


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
            record_path = server._save_call_record(record)
            observed: dict[str, object] = {}

            async def _fake_run_post_call(caller_id: str, paths: list[Path]) -> None:
                observed["caller_id"] = caller_id
                observed["paths"] = [str(path) for path in paths]

            server._run_post_call_command = _fake_run_post_call  # type: ignore[method-assign]

            await server._schedule_post_call(record.caller_id, [record_path])
            task = server._pending_post_calls[record.caller_id]
            await task

            assert observed == {
                "caller_id": "+46700000001",
                "paths": [str(record_path)],
            }

    asyncio.run(_exercise())


def test_load_recent_call_falls_back_to_legacy_flat_path() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        record = RecentCallRecord(
            caller_id="+46700000009",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="Legacy state")],
            metadata={},
        )
        legacy_path = server._legacy_recent_call_path(record.caller_id)
        legacy_path.write_text(
            json.dumps(
                {
                    "caller_id": record.caller_id,
                    "source": record.source,
                    "ended_at": record.ended_at,
                    "transcript": [dict(role="user", text="Legacy state")],
                    "metadata": {},
                }
            )
        )

        loaded = server._load_recent_call(record.caller_id)

        assert loaded is not None
        assert loaded.transcript[0].text == "Legacy state"


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


def test_consume_recent_call_keeps_archived_record() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        server.resume_window_seconds = 300
        record = RecentCallRecord(
            caller_id="+46700000005",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="Archive me")],
            metadata={"call_sid": "CAarchived"},
        )
        archived_path = server._save_call_record(record)
        server._save_recent_call(record)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_100.0)
            server._consume_recent_call(record.caller_id)

        assert archived_path.exists()
        payload = json.loads(archived_path.read_text())
        assert payload["transcript"][0]["text"] == "Archive me"


def test_resume_carries_prior_archive_into_next_post_call() -> None:
    async def _exercise() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = VoiceServer()
            server.state_dir = Path(tmpdir)
            server.resume_window_seconds = 300
            server.post_call_command = "run-post-call"
            server.post_call_delay_seconds = 1_000

            first = RecentCallRecord(
                caller_id="+46700000010",
                source="twilio",
                ended_at=1_000.0,
                transcript=[TranscriptTurn(role="user", text="first leg")],
                metadata={"call_sid": "CAfirst"},
            )
            first_path = server._save_call_record(first)
            server._save_recent_call(first)
            await server._schedule_post_call(first.caller_id, [first_path])
            first_task = server._pending_post_calls[first.caller_id]

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_100.0)
                resumed = server._consume_recent_call(first.caller_id)
            await asyncio.sleep(0)

            assert resumed is not None
            assert first_task.cancelled()
            assert server._pending_archive_records[first.caller_id] == [first_path]

            second = RecentCallRecord(
                caller_id=first.caller_id,
                source="twilio",
                ended_at=1_200.0,
                transcript=[TranscriptTurn(role="user", text="second leg")],
                metadata={"call_sid": "CAsecond"},
            )
            second_path = server._save_call_record(second)
            await server._schedule_post_call(first.caller_id, [first_path, second_path])

            assert server._pending_archive_records[first.caller_id] == [
                first_path,
                second_path,
            ]

            server._pending_post_calls[first.caller_id].cancel()

    asyncio.run(_exercise())


def test_save_call_record_uses_unique_archive_path_per_call() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)

        first = RecentCallRecord(
            caller_id="+46700000006",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="First call")],
            metadata={"call_sid": "CAfirst"},
        )
        second = RecentCallRecord(
            caller_id="+46700000006",
            source="twilio",
            ended_at=1_001.0,
            transcript=[TranscriptTurn(role="user", text="Second call")],
            metadata={"call_sid": "CAsecond"},
        )

        first_path = server._save_call_record(first)
        second_path = server._save_call_record(second)

        assert first_path != second_path
        assert first_path.exists()
        assert second_path.exists()
        assert (
            json.loads(first_path.read_text())["transcript"][0]["text"] == "First call"
        )
        assert (
            json.loads(second_path.read_text())["transcript"][0]["text"]
            == "Second call"
        )


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
            await server._schedule_post_call(record.caller_id, [record_path])
            first_task = server._pending_post_calls[record.caller_id]

            # Schedule second task — cancels first and registers itself
            await server._schedule_post_call(record.caller_id, [record_path])
            second_task = server._pending_post_calls[record.caller_id]

            # Wait for the first task's finally-block to run
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            # The second task must still be registered
            assert server._pending_post_calls.get(record.caller_id) is second_task
            assert first_task.cancelled()

            second_task.cancel()

    asyncio.run(_exercise())


def test_cancelled_post_call_command_terminates_subprocess() -> None:
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _exercise() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = VoiceServer()
            server.state_dir = Path(tmpdir)
            pid_file = Path(tmpdir) / "post-call.pid"
            record_path = Path(tmpdir) / "recent-call.json"
            record_path.write_text("{}")
            script = (
                "import os, pathlib, time; "
                "pathlib.Path(os.environ['PID_FILE']).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            server.post_call_command = (
                f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            )

            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("PID_FILE", str(pid_file))
                task = asyncio.create_task(
                    server._run_post_call_command("+46700000004", [record_path])
                )

                deadline = asyncio.get_running_loop().time() + 5
                while not pid_file.exists():
                    if asyncio.get_running_loop().time() > deadline:
                        raise RuntimeError("post-call command did not start")
                    await asyncio.sleep(0.05)

                pid = int(pid_file.read_text())
                assert _pid_exists(pid)

                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                deadline = asyncio.get_running_loop().time() + 5
                while _pid_exists(pid):
                    if asyncio.get_running_loop().time() > deadline:
                        raise AssertionError(
                            "cancelled post-call command subprocess still running"
                        )
                    await asyncio.sleep(0.05)

    asyncio.run(_exercise())


class _ClosedIterTextWebSocket:
    """WebSocket stub whose iter_text raises the starlette 'already closed' error.

    Reproduces the condition observed after ``_schedule_hangup`` closes the socket
    server-side: the next call into ``iter_text`` (which calls ``receive_text``)
    sees ``application_state != CONNECTED`` and raises ``RuntimeError`` instead of
    ``WebSocketDisconnect``.
    """

    def __init__(self, error: RuntimeError) -> None:
        self._error = error
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    def iter_text(self):
        async def _gen():
            raise self._error
            yield  # pragma: no cover - generator marker

        return _gen()

    @property
    def query_params(self) -> dict[str, str]:
        return {}


def test_twilio_handler_swallows_runtimeerror_after_server_close(tmp_path) -> None:
    """After _schedule_hangup closes the socket, iter_text raises RuntimeError.

    The handler should treat a 'not connected' RuntimeError as a normal
    disconnect (equivalent to WebSocketDisconnect) instead of logging a
    traceback. Regression test for the noise observed in production logs:
    'Error handling Twilio connection: WebSocket is not connected.'
    """
    server = VoiceServer()
    server.state_dir = tmp_path
    websocket = _ClosedIterTextWebSocket(
        RuntimeError('WebSocket is not connected. Need to call "accept" first.')
    )

    # Should not raise — the RuntimeError must be swallowed like WebSocketDisconnect.
    asyncio.run(server.handle_twilio_websocket(websocket))

    assert websocket.accepted is True


def test_twilio_handler_reraises_unrelated_runtimeerror(tmp_path) -> None:
    """Only the starlette 'not connected' RuntimeError should be swallowed.

    Unrelated RuntimeErrors must still surface so real bugs are not hidden.
    """
    server = VoiceServer()
    server.state_dir = tmp_path
    websocket = _ClosedIterTextWebSocket(RuntimeError("unexpected failure"))

    with pytest.raises(RuntimeError, match="unexpected failure"):
        asyncio.run(server.handle_twilio_websocket(websocket))
