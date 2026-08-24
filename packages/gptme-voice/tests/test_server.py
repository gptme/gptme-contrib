import asyncio
import base64
import json
import os
import shlex
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
from gptme_voice.realtime.audio import AudioConverter
from gptme_voice.realtime.server import (
    _VOICE_DIGEST_MAX_AGE_SECONDS,
    RecentCallRecord,
    SessionBootstrap,
    TranscriptTurn,
    VoiceServer,
    _append_transcript_turn,
    _assistant_committed_hangup,
    _build_caller_instructions,
    _build_fresh_call_greeting_instructions,
    _build_resume_instructions,
    _build_runtime_identity_instructions,
    _get_twilio_field,
    _load_voice_digest,
    _lookup_caller_identity,
    _prepend_activity_digest,
    _should_trigger_hangup_transcript_fallback,
    _truncate_resume_transcript,
)
from gptme_voice.realtime.sounds import PCM_CUES, SAMPLE_RATE


class _DummyWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.binary_messages: list[bytes] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(message)

    async def send_bytes(self, message: bytes) -> None:
        self.binary_messages.append(message)


class _ClosableWebSocket(_DummyWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _DummyBrowserWebSocket:
    def __init__(self, incoming: list[dict[str, object]]) -> None:
        self.query_params: dict[str, str] = {}
        self._incoming = incoming
        self.accepted = False
        self.text_messages: list[str] = []
        self.binary_messages: list[bytes] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, object]:
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, message: str) -> None:
        self.text_messages.append(message)

    async def send_bytes(self, message: bytes) -> None:
        self.binary_messages.append(message)


class _DummyTwilioWebSocket(_DummyWebSocket):
    def __init__(self, incoming: list[dict[str, object]]) -> None:
        super().__init__()
        self._incoming = [json.dumps(message) for message in incoming]
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    def iter_text(self):
        async def _gen():
            for message in self._incoming:
                yield message

        return _gen()

    @property
    def query_params(self) -> dict[str, str]:
        return {}


class _FakeRealtimeClient:
    def __init__(self) -> None:
        self.sent_audio: list[bytes] = []
        self.commit_count = 0
        self.disconnect_kwargs: dict[str, object] | None = None
        self.activate_session_count = 0
        self.on_function_call = None

    async def connect(self) -> None:
        return None

    async def activate_session(self) -> None:
        self.activate_session_count += 1

    async def send_audio(self, audio_data: bytes) -> None:
        self.sent_audio.append(audio_data)

    async def commit_audio(self) -> None:
        self.commit_count += 1

    async def disconnect(self, **kwargs: object) -> None:
        self.disconnect_kwargs = kwargs

    async def inject_message(self, _text: str) -> None:
        return None


class _DummyToolBridge:
    def __init__(self, *args, **kwargs) -> None:
        return None

    async def handle_function_call(self, _name: str, _args: dict) -> None:
        return None

    def pending_task_ids(self) -> list[str]:
        return []

    def get_timings(self) -> list[dict[str, object]]:
        return []


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


def test_build_runtime_identity_instructions_grok() -> None:
    result = _build_runtime_identity_instructions("grok", None)
    assert "Grok" in result
    assert "xAI" in result
    # Must explicitly forbid the observed confabulation.
    assert "Claude" in result
    assert "GPT" in result
    assert "truthfully" in result.lower()
    # No model alias supplied → no model clause.
    assert "model:" not in result


def test_build_runtime_identity_instructions_openai_with_model() -> None:
    result = _build_runtime_identity_instructions("openai", "gpt-realtime")
    assert "OpenAI" in result
    assert "gpt-realtime" in result
    assert "model: gpt-realtime" in result


def test_build_runtime_identity_instructions_unknown_provider() -> None:
    # Unknown provider should still produce a non-confabulating block.
    result = _build_runtime_identity_instructions("anthropic", None)
    assert "anthropic" in result


def test_voice_server_prepends_runtime_identity_to_instructions() -> None:
    server = VoiceServer(provider="grok")
    # Stable persona identity leads the prompt, followed by runtime provider identity.
    assert server._instructions.startswith("IDENTITY: You are Bob.")
    assert "RUNTIME IDENTITY:" in server._instructions
    assert "Grok" in server._instructions


def test_lookup_caller_identity_uses_call_name_when_present() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        people_dir = Path(tmpdir) / "people"
        people_dir.mkdir()
        (people_dir / "erik-bjareholt.md").write_text(
            "# Erik Bjäreholt\n\n- Call name: Erik\nPhone: +46700000001\n"
        )

        identity = _lookup_caller_identity("+46700000001", tmpdir)

    assert identity is not None
    assert identity.canonical_name == "Erik Bjäreholt"
    assert identity.preferred_spoken_name == "Erik"


def test_get_twilio_field_prefers_camel_case() -> None:
    payload = {"streamSid": "MZ123", "stream_sid": "legacy"}

    assert _get_twilio_field(payload, "streamSid", "stream_sid") == "MZ123"


def test_get_twilio_field_falls_back_to_snake_case() -> None:
    payload = {"stream_sid": "legacy"}

    assert _get_twilio_field(payload, "streamSid", "stream_sid") == "legacy"


def test_assistant_committed_hangup_matches_live_failure_phrases() -> None:
    assert _assistant_committed_hangup(
        "You're welcome Erik, have a great day. I'll hang up now."
    )
    assert _assistant_committed_hangup(
        "One moment. I'll call the hangup tool to end the call."
    )
    assert _assistant_committed_hangup("One moment. Calling hangup tool now.")


def test_assistant_committed_hangup_rejects_conditional_offer() -> None:
    assert not _assistant_committed_hangup("I can hang up if you'd like.")
    assert not _assistant_committed_hangup("Would you like me to end the call?")


def test_hangup_transcript_fallback_requires_recent_user_end_intent() -> None:
    assert _should_trigger_hangup_transcript_fallback(
        [TranscriptTurn(role="user", text="Yeah, thank you. Bye.")],
        "You're welcome Erik, have a great day. I'll hang up now.",
    )
    assert not _should_trigger_hangup_transcript_fallback(
        [TranscriptTurn(role="user", text="Can you tell me more about the registry?")],
        "I'll hang up now.",
    )


def test_make_transcript_callbacks_schedule_hangup_fallback_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = VoiceServer()
    websocket = _ClosableWebSocket()
    transcript = [TranscriptTurn(role="user", text="Yeah, thank you. Bye.")]

    async def _exercise() -> None:
        release = asyncio.Event()
        calls: list[dict[str, object]] = []

        async def _fake_schedule_hangup(
            websocket_arg, *, source: str, reason: str | None, call_sid: str | None
        ) -> None:
            calls.append(
                {
                    "websocket": websocket_arg,
                    "source": source,
                    "reason": reason,
                    "call_sid": call_sid,
                }
            )
            await release.wait()

        monkeypatch.setattr(server, "_schedule_hangup", _fake_schedule_hangup)
        on_ai_transcript, _on_user_transcript, _on_hangup = (
            server._make_transcript_callbacks(
                transcript=transcript,
                websocket=websocket,
                source="twilio",
                call_sid="CA123",
            )
        )

        await on_ai_transcript(
            "You're welcome Erik, have a great day. I'll hang up now."
        )
        await asyncio.sleep(0)
        await on_ai_transcript("One moment. Calling hangup tool now.")
        await asyncio.sleep(0)

        assert transcript[-1].text == "One moment. Calling hangup tool now."
        assert calls == [
            {
                "websocket": websocket,
                "source": "twilio:transcript-fallback",
                "reason": (
                    "assistant said: You're welcome Erik, have a great day. "
                    "I'll hang up now."
                ),
                "call_sid": "CA123",
            }
        ]
        release.set()
        await asyncio.sleep(0)

    asyncio.run(_exercise())


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


def test_sound_cue_callback_sends_playable_pcm_payload() -> None:
    async def _exercise() -> None:
        websocket = _DummyWebSocket()
        callback = VoiceServer._make_sound_cue_callback(websocket, "dispatch")

        await callback()

        message = json.loads(websocket.messages[0])
        assert message["type"] == "sound_cue"
        assert message["cue"] == "dispatch"
        assert message["sample_rate"] == SAMPLE_RATE
        assert base64.b64decode(message["audio"]) == PCM_CUES["dispatch"]

    asyncio.run(_exercise())


def test_build_session_config_passes_reasoning_effort_override() -> None:
    server = VoiceServer(reasoning_effort="minimal")

    session_config = server._build_session_config("You are Bob.")

    assert session_config.reasoning_effort == "minimal"


def test_voice_route_disabled_by_default() -> None:
    server = VoiceServer()

    websocket_paths = {
        route.path for route in server.app.routes if hasattr(route, "path")
    }

    assert "/voice" not in websocket_paths


def test_voice_route_enabled_when_requested() -> None:
    server = VoiceServer(enable_browser_transport=True)

    websocket_paths = {
        route.path for route in server.app.routes if hasattr(route, "path")
    }

    assert "/voice" in websocket_paths


def test_send_browser_audio_uses_binary_frames() -> None:
    server = VoiceServer(enable_browser_transport=True)
    websocket = _DummyWebSocket()

    asyncio.run(server._send_browser_audio(websocket, b"\x00\x01"))

    assert websocket.binary_messages == [b"\x00\x01"]


def test_send_browser_audio_end_uses_text_control_message() -> None:
    server = VoiceServer(enable_browser_transport=True)
    websocket = _DummyWebSocket()

    asyncio.run(server._send_browser_audio_end(websocket))

    assert [json.loads(message) for message in websocket.messages] == [
        {"type": "audio_end"}
    ]


def test_handle_browser_websocket_resamples_binary_pcm_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = VoiceServer(enable_browser_transport=True)
    websocket = _DummyBrowserWebSocket(
        [
            {"type": "websocket.receive", "bytes": b"\x00\x00\x01\x00"},
            {"type": "websocket.receive", "text": json.dumps({"type": "commit"})},
            {"type": "websocket.disconnect"},
        ]
    )
    fake_client = _FakeRealtimeClient()

    async def _fake_build_session_instructions(
        *, caller_id: str, handoff_id: str | None
    ) -> str:
        return "You are Bob."

    def _fake_make_client(_session_cfg, **_kwargs):
        return fake_client

    async def _fake_on_call_end(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        server, "_build_session_instructions", _fake_build_session_instructions
    )
    monkeypatch.setattr(server, "_make_client", _fake_make_client)
    monkeypatch.setattr(server, "_on_call_end", _fake_on_call_end)
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)

    asyncio.run(server.handle_browser_websocket(websocket))

    expected_audio = AudioConverter().browser_to_openai(b"\x00\x00\x01\x00")
    assert websocket.accepted is True
    assert json.loads(websocket.text_messages[0]) == {
        "type": "ready",
        "input_sample_rate": AudioConverter.BROWSER_RATE,
        "output_sample_rate": AudioConverter.OPENAI_RATE,
    }
    assert fake_client.sent_audio == [expected_audio]
    assert fake_client.commit_count == 1
    assert fake_client.disconnect_kwargs is not None
    assert fake_client.disconnect_kwargs["commit_audio"] is True


def test_handle_twilio_websocket_wires_speech_started_clear_callback_cold_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _exercise() -> None:
        server = VoiceServer()
        websocket = _DummyTwilioWebSocket(
            [
                {"event": "connected"},
                {
                    "event": "start",
                    "start": {
                        "streamSid": "MZ123",
                        "callSid": "CA123",
                        "customParameters": {},
                    },
                },
                {"event": "stop"},
            ]
        )
        fake_client = _FakeRealtimeClient()
        captured_kwargs: dict[str, object] = {}

        async def _fake_build_session_bootstrap(
            *,
            caller_id: str,
            from_number: str = "",
            handoff_id: str | None = None,
            standup_brief: str | None = None,
        ) -> SessionBootstrap:
            return SessionBootstrap("You are Bob.")

        def _fake_make_client(_session_cfg, **kwargs):
            captured_kwargs.update(kwargs)
            return fake_client

        async def _fake_on_call_end(*args, **kwargs) -> None:
            return None

        monkeypatch.setattr(
            server, "_build_session_bootstrap", _fake_build_session_bootstrap
        )
        monkeypatch.setattr(server, "_make_client", _fake_make_client)
        monkeypatch.setattr(server, "_on_call_end", _fake_on_call_end)
        monkeypatch.setattr(
            "gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge
        )

        await server.handle_twilio_websocket(websocket)

        assert websocket.accepted is True
        assert "on_speech_started" in captured_kwargs

        on_speech_started = captured_kwargs["on_speech_started"]
        assert callable(on_speech_started)
        await on_speech_started()

        assert {"event": "clear", "streamSid": "MZ123"} in [
            json.loads(m) for m in websocket.messages
        ]

    asyncio.run(_exercise())


def test_handle_twilio_websocket_rebinds_speech_started_clear_callback_on_prewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _exercise() -> None:
        server = VoiceServer()
        websocket = _DummyTwilioWebSocket(
            [
                {"event": "connected"},
                {
                    "event": "start",
                    "start": {
                        "streamSid": "MZ123",
                        "callSid": "CA123",
                        "customParameters": {"from_number": "+46700000001"},
                    },
                },
                {"event": "stop"},
            ]
        )
        fake_client = _FakeRealtimeClient()

        async def _fake_on_call_end(*args, **kwargs) -> None:
            return None

        monkeypatch.setattr(server, "_claim_prewarm", lambda _from_number: fake_client)
        monkeypatch.setattr(server, "_on_call_end", _fake_on_call_end)
        monkeypatch.setattr(
            "gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge
        )

        await server.handle_twilio_websocket(websocket)

        assert websocket.accepted is True
        assert fake_client.activate_session_count == 1
        assert callable(fake_client.on_speech_started)

        await fake_client.on_speech_started()

        assert {"event": "clear", "streamSid": "MZ123"} in [
            json.loads(m) for m in websocket.messages
        ]

    asyncio.run(_exercise())


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

    bootstrap = asyncio.run(
        server._build_session_bootstrap(
            caller_id="+46700000011",
            from_number="+46700000011",
        )
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

        bootstrap = asyncio.run(
            server._build_session_bootstrap(
                caller_id="+46700000001",
                from_number="+46700000001",
            )
        )

    assert bootstrap.should_greet_first is True
    assert "You are Bob" in bootstrap.initial_response_instructions
    assert "Erik Bjäreholt" in bootstrap.initial_response_instructions
    assert (
        "using 'Erik', not their full name" in bootstrap.initial_response_instructions
    )
    assert "Do NOT say 'thanks for calling'" in bootstrap.initial_response_instructions


def test_build_session_bootstrap_avoids_full_name_warning_for_single_token_name() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmpdir:
        people_dir = Path(tmpdir) / "people"
        people_dir.mkdir()
        (people_dir / "erik.md").write_text("# Erik\n\nPhone: +46700000002\n")
        server = VoiceServer(workspace=tmpdir)
        server._instructions = "You are Bob."

        bootstrap = asyncio.run(
            server._build_session_bootstrap(
                caller_id="+46700000002",
                from_number="+46700000002",
            )
        )

    assert bootstrap.should_greet_first is True
    assert (
        "The caller is Erik. Greet them by name"
        in bootstrap.initial_response_instructions
    )
    assert "not their full name" not in bootstrap.initial_response_instructions


def test_build_session_bootstrap_asks_unknown_caller_to_identify() -> None:
    server = VoiceServer()
    server._instructions = "You are Bob."

    bootstrap = asyncio.run(
        server._build_session_bootstrap(
            caller_id="+15551234567",
            from_number="+15551234567",
        )
    )

    assert bootstrap.should_greet_first is True
    assert "You are Bob" in bootstrap.initial_response_instructions
    assert "caller is unknown" in bootstrap.initial_response_instructions
    assert "Hello, this is Bob" in bootstrap.initial_response_instructions
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
            resumed = asyncio.run(server._consume_recent_call("+46700000001"))

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
            bootstrap = asyncio.run(
                server._build_session_bootstrap(caller_id=record.caller_id)
            )

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
            resumed = asyncio.run(server._consume_recent_call("+46700000001"))

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
            instructions = asyncio.run(
                server._build_session_instructions(
                    caller_id="+46700000007",
                    handoff_id="handoff-123",
                )
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
            instructions = asyncio.run(
                server._build_session_instructions(
                    caller_id=record.caller_id,
                    handoff_id="handoff-stale",
                )
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

            async def _fake_run_post_call(
                caller_id: str,
                paths: list[Path],
                *,
                delay_seconds: int = 0,
                unit_name: str | None = None,
            ) -> None:
                observed["caller_id"] = caller_id
                observed["paths"] = [str(path) for path in paths]
                observed["delay_seconds"] = delay_seconds
                observed["unit_name"] = unit_name

            server._run_post_call_command = _fake_run_post_call  # type: ignore[method-assign]

            await server._schedule_post_call(record.caller_id, [record_path])

            assert observed == {
                "caller_id": "+46700000001",
                "paths": [str(record_path)],
                "delay_seconds": 0,
                "unit_name": server._pending_post_calls[record.caller_id],
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
            asyncio.run(server._consume_recent_call("+46700000002"))

        assert not state_path.exists()


def test_prewarm_connect_failure_preserves_resume_state() -> None:
    """P1 fix: if _prewarm_for_inbound's connect() raises, the on-disk resume
    state file must NOT be deleted so the cold-path _build_session_bootstrap
    can still resume the caller normally."""
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        server.resume_window_seconds = 300
        record = RecentCallRecord(
            caller_id="+46700000099",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="Keep this resume")],
            metadata={},
        )
        server._save_recent_call(record)
        state_path = server._recent_call_path("+46700000099")
        assert state_path.exists()

        # Simulate a connect() failure inside _prewarm_for_inbound
        class _FailingClient:
            async def connect(self):
                raise ConnectionError("simulated provider failure")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                server,
                "_make_client",
                lambda *args, **kwargs: _FailingClient(),  # type: ignore[assignment]
            )
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_100.0)
            asyncio.run(server._prewarm_for_inbound("+46700000099"))

        # Resume file must survive the failed prewarm
        assert state_path.exists(), (
            "Resume state file was deleted despite connect() failure — "
            "caller would get a fresh greeting instead of resuming"
        )

        # And the cold path must still see the resume context
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_200.0)
            bootstrap = asyncio.run(
                server._build_session_bootstrap(caller_id="+46700000099")
            )
        assert (
            not bootstrap.should_greet_first
        ), "Cold-path bootstrap should resume (no greeting) after failed prewarm"


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
            asyncio.run(server._consume_recent_call(record.caller_id))

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
            cancelled_units: list[str] = []

            async def _fake_run_post_call(
                caller_id: str,
                paths: list[Path],
                *,
                delay_seconds: int = 0,
                unit_name: str | None = None,
            ) -> None:
                return None

            server._run_post_call_command = _fake_run_post_call  # type: ignore[method-assign]
            server._cancel_post_call_schedule = cancelled_units.append  # type: ignore[method-assign]

            first = RecentCallRecord(
                caller_id="+46700000010",
                source="twilio",
                ended_at=1_000.0,
                transcript=[TranscriptTurn(role="user", text="first leg")],
                metadata={"call_sid": "CAfirst"},
            )
            first_path = server._save_call_record(first)
            await server._schedule_post_call(first.caller_id, [first_path])
            first_group_id = server._pending_call_groups[first.caller_id]
            first_unit = server._pending_post_calls[first.caller_id]
            first.archive_record_paths = [str(first_path)]
            first.pending_post_call_unit = first_unit
            first.call_group_id = first_group_id
            server._save_recent_call(first)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_100.0)
                resumed = await server._consume_recent_call(first.caller_id)

            assert resumed is not None
            assert cancelled_units == [first_unit]
            assert server._pending_archive_records[first.caller_id] == [first_path]
            assert server._pending_call_groups[first.caller_id] == first_group_id

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
            assert server._pending_call_groups[first.caller_id] == first_group_id
            manifest = json.loads(
                server._call_group_manifest_path(first_group_id).read_text()
            )
            assert manifest["status"] == "open"
            assert manifest["remote_party"] == first.caller_id
            assert manifest["archive_record_paths"] == [
                str(first_path),
                str(second_path),
            ]
            assert server._pending_post_calls[first.caller_id] != first_unit

    asyncio.run(_exercise())


def test_call_record_payload_preserves_remote_party_identity() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = VoiceServer()
        server.state_dir = Path(tmpdir)
        record = RecentCallRecord(
            caller_id="+46700000001",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="hello")],
            metadata={
                "call_sid": "CAoutbound",
                "from_number": "+15551234567",
                "remote_party": "+46700000001",
            },
        )

        path = server._save_call_record(record)
        payload = json.loads(path.read_text())

        assert payload["caller_id"] == "+46700000001"
        assert "call_group_id" not in payload
        assert payload["metadata"]["remote_party"] == "+46700000001"


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


def test_schedule_post_call_replaces_existing_timer_unit() -> None:
    """Rescheduling a caller should cancel the old transient timer and keep the new one."""

    async def _exercise() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = VoiceServer()
            server.state_dir = Path(tmpdir)
            server.post_call_command = "run-post-call"
            server.post_call_delay_seconds = 1_000
            cancelled_units: list[str] = []
            scheduled_units: list[str] = []

            async def _fake_run_post_call(
                caller_id: str,
                paths: list[Path],
                *,
                delay_seconds: int = 0,
                unit_name: str | None = None,
            ) -> None:
                if unit_name is not None:
                    scheduled_units.append(unit_name)

            server._run_post_call_command = _fake_run_post_call  # type: ignore[method-assign]
            server._cancel_post_call_schedule = cancelled_units.append  # type: ignore[method-assign]

            record = RecentCallRecord(
                caller_id="+46700000003",
                source="twilio",
                ended_at=1_000.0,
                transcript=[],
                metadata={},
            )
            record_path = server._save_call_record(record)
            second_path = server._save_call_record(
                RecentCallRecord(
                    caller_id=record.caller_id,
                    source="twilio",
                    ended_at=1_001.0,
                    transcript=[],
                    metadata={},
                )
            )

            await server._schedule_post_call(record.caller_id, [record_path])
            first_unit = server._pending_post_calls[record.caller_id]

            await server._schedule_post_call(
                record.caller_id, [record_path, second_path]
            )
            second_unit = server._pending_post_calls[record.caller_id]

            assert cancelled_units == [first_unit]
            assert second_unit != first_unit
            assert scheduled_units == [first_unit, second_unit]
            assert server._pending_post_calls.get(record.caller_id) == second_unit

    asyncio.run(_exercise())


def test_consume_recent_call_restores_pending_schedule_after_restart() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        first_server = VoiceServer()
        first_server.state_dir = Path(tmpdir)
        record = RecentCallRecord(
            caller_id="+46700000012",
            source="twilio",
            ended_at=1_000.0,
            transcript=[TranscriptTurn(role="user", text="Restart-safe resume")],
            metadata={"call_sid": "CArestart"},
        )
        record_path = first_server._save_call_record(record)
        record.archive_record_paths = [str(record_path)]
        record.pending_post_call_unit = "gptme-voice-post-call-restart"
        first_server._save_recent_call(record)

        second_server = VoiceServer()
        second_server.state_dir = Path(tmpdir)
        cancelled_units: list[str] = []
        second_server._cancel_post_call_schedule = cancelled_units.append  # type: ignore[method-assign]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("gptme_voice.realtime.server.time.time", lambda: 1_100.0)
            resumed = asyncio.run(second_server._consume_recent_call(record.caller_id))

        assert resumed is not None
        assert cancelled_units == ["gptme-voice-post-call-restart"]
        assert second_server._pending_archive_records[record.caller_id] == [record_path]


def test_on_call_end_persists_pending_post_call_state() -> None:
    async def _exercise() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = VoiceServer()
            server.state_dir = Path(tmpdir)
            server.post_call_command = "run-post-call"
            server.post_call_delay_seconds = 300

            async def _fake_run_post_call(
                caller_id: str,
                paths: list[Path],
                *,
                delay_seconds: int = 0,
                unit_name: str | None = None,
            ) -> None:
                return None

            server._run_post_call_command = _fake_run_post_call  # type: ignore[method-assign]

            await server._on_call_end(
                caller_id="+46700000013",
                source="twilio",
                transcript=[TranscriptTurn(role="user", text="Persist the chain")],
                metadata={"call_sid": "CApersist"},
            )

            recent = server._load_recent_call("+46700000013")
            assert recent is not None
            assert recent.call_group_id == server._pending_call_groups["+46700000013"]
            assert len(recent.archive_record_paths) == 1
            assert (
                recent.pending_post_call_unit
                == server._pending_post_calls["+46700000013"]
            )
            assert Path(recent.archive_record_paths[0]).exists()
            manifest = json.loads(
                server._call_group_manifest_path(recent.call_group_id).read_text()
            )
            assert manifest["remote_party"] == "+46700000013"
            assert manifest["archive_record_paths"] == recent.archive_record_paths

    asyncio.run(_exercise())


def test_post_call_schedule_survives_scheduler_process_exit(tmp_path: Path) -> None:
    marker_file = tmp_path / "post-call-fired.txt"
    launcher_done_file = tmp_path / "launcher-done.txt"
    wrapper_path = tmp_path / "fake_post_call_wrapper.py"
    launcher_path = tmp_path / "schedule_call.py"

    wrapper_path.write_text(
        textwrap.dedent(
            """\
import os
import subprocess
import sys

delay_seconds = float(os.environ.get("GPTME_VOICE_POST_CALL_DELAY_SECONDS", "0"))
marker_path = os.environ["MARKER_FILE"]
launcher_done_path = os.environ["LAUNCHER_DONE_FILE"]
record_path = sys.argv[1]
payload = os.environ.get("GPTME_VOICE_POST_CALL_UNIT_NAME", record_path)
child_code = '''
import pathlib
import sys
import time

done_path = pathlib.Path(sys.argv[1])
while not done_path.exists():
    time.sleep(0.05)
time.sleep(float(sys.argv[2]))
pathlib.Path(sys.argv[3]).write_text(sys.argv[4])
'''
subprocess.Popen(
    [
        sys.executable,
        "-c",
        child_code,
        launcher_done_path,
        str(delay_seconds),
        marker_path,
        payload,
    ],
    close_fds=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
"""
        )
    )

    launcher_path.write_text(
        textwrap.dedent(
            """\
import asyncio
import shlex
import sys
from pathlib import Path

from gptme_voice.realtime.server import TranscriptTurn, VoiceServer

async def main() -> None:
    server = VoiceServer()
    server.state_dir = Path(sys.argv[1])
    server.post_call_command = (
        f"{sys.executable} {shlex.quote(str(Path(sys.argv[2])))}"
    )
    server.post_call_delay_seconds = 1
    await server._on_call_end(
        caller_id="+46700000014",
        source="twilio",
        transcript=[TranscriptTurn(role="user", text="Stay durable")],
        metadata={"call_sid": "CAsubprocess"},
    )
    Path(sys.argv[3]).write_text("done")

asyncio.run(main())
"""
        )
    )

    env = os.environ.copy()
    env["MARKER_FILE"] = str(marker_file)
    env["LAUNCHER_DONE_FILE"] = str(launcher_done_file)
    result = os.spawnve(
        os.P_WAIT,
        sys.executable,
        [
            sys.executable,
            str(launcher_path),
            str(tmp_path),
            str(wrapper_path),
            str(launcher_done_file),
        ],
        env,
    )

    assert result == 0
    assert launcher_done_file.exists()
    assert not marker_file.exists()

    deadline = time.time() + 15
    while time.time() < deadline and not marker_file.exists():
        time.sleep(0.05)

    assert marker_file.exists()
    assert marker_file.read_text().startswith("gptme-voice-post-call-")


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


def test_browser_websocket_ignores_malformed_json_text_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed JSON text frame must not kill the browser voice session (P1 fix)."""
    server = VoiceServer(enable_browser_transport=True)
    websocket = _DummyBrowserWebSocket(
        [
            {"type": "websocket.receive", "text": "not-valid-json"},
            {"type": "websocket.receive", "text": json.dumps({"type": "commit"})},
            {"type": "websocket.disconnect"},
        ]
    )
    fake_client = _FakeRealtimeClient()

    async def _fake_build_session_instructions(
        *, caller_id: str, handoff_id: str | None
    ) -> str:
        return "You are Bob."

    def _fake_make_client(_session_cfg, **_kwargs):
        return fake_client

    async def _fake_on_call_end(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        server, "_build_session_instructions", _fake_build_session_instructions
    )
    monkeypatch.setattr(server, "_make_client", _fake_make_client)
    monkeypatch.setattr(server, "_on_call_end", _fake_on_call_end)
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)

    # Should not raise — malformed frame is skipped, commit still fires
    asyncio.run(server.handle_browser_websocket(websocket))

    assert fake_client.commit_count == 1


def test_audio_converter_independent_resample_states() -> None:
    """Twilio (8kHz) and Browser (16kHz) upsampling must not share resampler state (P2 fix).

    After priming the Twilio (8kHz→24kHz) path, Browser (16kHz→24kHz) output must
    be identical to a fresh converter that has never seen Twilio audio. If the two
    paths shared one _resample_state the state from the 8kHz path would corrupt
    the 16kHz conversion.
    """
    twilio_pcm = b"\x00\x00" * 160  # 160 samples @ 8kHz = 20ms
    browser_pcm = (
        b"\x01\x00" * 320
    )  # 320 samples @ 16kHz = 20ms (non-zero to detect corruption)

    # Converter that sees both paths interleaved
    combined = AudioConverter()
    combined.twilio_to_openai(twilio_pcm)  # prime 8kHz state
    combined_browser_out = combined.browser_to_openai(browser_pcm)

    # Fresh converter that has only ever seen browser audio
    fresh = AudioConverter()
    fresh_browser_out = fresh.browser_to_openai(browser_pcm)

    # With per-rate state slots the browser path starts fresh regardless of prior
    # Twilio calls — outputs must be identical.
    assert combined_browser_out == fresh_browser_out


# ── transcript promotion (Phase 3 Step 2) ──────────────────────────────────


class TestTranscriptPromotion:
    """Voice Phase 3: transcript → gptme conversation log."""

    def test_promote_transcript_posts_to_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transcript with turns is POSTed to the gptme server."""
        calls: list[tuple[str, bytes, str]] = []

        def _fake_post_sync(url: str, payload: bytes, api_key: str) -> None:
            calls.append((url, payload, api_key))

        monkeypatch.setattr(
            "gptme_voice.realtime.server._http_post_sync", _fake_post_sync
        )
        monkeypatch.setenv("GPTME_VOICE_GPTME_SERVER_URL", "https://gptme.ai")
        monkeypatch.setenv("GPTME_VOICE_GPTME_SERVER_KEY", "test-key")

        server = VoiceServer()
        transcript = [
            TranscriptTurn(role="user", text="Hello"),
            TranscriptTurn(role="assistant", text="Hi there"),
        ]
        metadata = {
            "call_sid": "CAabc123",
            "from": "+15551234567",
        }

        server._promote_transcript_to_gptme("+15551234567", transcript, metadata)

        assert len(calls) == 1
        url, payload, key = calls[0]
        assert url == "https://gptme.ai/api/v2/conversations/%2B15551234567/transcript"
        assert key == "test-key"

        body = json.loads(payload)
        assert body["call_metadata"]["call_sid"] == "CAabc123"
        assert len(body["turns"]) == 2
        assert body["turns"][0] == {"role": "user", "text": "Hello"}
        assert body["turns"][1] == {"role": "assistant", "text": "Hi there"}

    def test_promote_skips_when_unconfigured(self) -> None:
        """When GPTME_VOICE_GPTME_SERVER_URL is empty, promotion is a no-op."""
        server = VoiceServer()
        assert server.gptme_server_url == ""

        transcript = [TranscriptTurn(role="user", text="Hello")]
        metadata = {"call_sid": "CAabc123"}

        # Should not raise — just return silently.
        server._promote_transcript_to_gptme("+15551234567", transcript, metadata)

    def test_promote_skips_empty_transcript(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transcript with no non-whitespace turns is skipped."""
        calls: list[tuple[str, bytes, str]] = []

        def _fake_post_sync(url: str, payload: bytes, api_key: str) -> None:
            calls.append((url, payload, api_key))

        monkeypatch.setattr(
            "gptme_voice.realtime.server._http_post_sync", _fake_post_sync
        )
        monkeypatch.setenv("GPTME_VOICE_GPTME_SERVER_URL", "https://gptme.ai")
        monkeypatch.setenv("GPTME_VOICE_GPTME_SERVER_KEY", "test-key")

        server = VoiceServer()
        transcript = [TranscriptTurn(role="user", text="   ")]
        metadata = {"call_sid": "CAabc123"}

        server._promote_transcript_to_gptme("+15551234567", transcript, metadata)
        assert len(calls) == 0

    def test_promote_skips_missing_call_sid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a call_sid in metadata, promotion is skipped."""
        calls: list[tuple[str, bytes, str]] = []

        def _fake_post_sync(url: str, payload: bytes, api_key: str) -> None:
            calls.append((url, payload, api_key))

        monkeypatch.setattr(
            "gptme_voice.realtime.server._http_post_sync", _fake_post_sync
        )
        monkeypatch.setenv("GPTME_VOICE_GPTME_SERVER_URL", "https://gptme.ai")
        monkeypatch.setenv("GPTME_VOICE_GPTME_SERVER_KEY", "test-key")

        server = VoiceServer()
        transcript = [TranscriptTurn(role="user", text="Hello")]
        metadata: dict[str, str] = {}  # no call_sid

        server._promote_transcript_to_gptme("+15551234567", transcript, metadata)
        assert len(calls) == 0


def test_server_g711_passthrough_off_by_default() -> None:
    server = VoiceServer()
    assert server.openai_g711_passthrough is False


def test_server_g711_passthrough_enabled_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPTME_VOICE_OPENAI_G711_PASSTHROUGH", "1")
    server = VoiceServer()
    assert server.openai_g711_passthrough is True

    session_config = server._build_session_config("You are Bob.")
    assert session_config.g711_passthrough is True
    assert session_config.input_format == "g711_ulaw"
    assert session_config.output_format == "g711_ulaw"


def test_server_g711_passthrough_ignored_for_grok_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPTME_VOICE_OPENAI_G711_PASSTHROUGH", "1")
    server = VoiceServer(provider="grok")
    # Grok requires PCM; the env flag must not bleed into a Grok session.
    assert server.openai_g711_passthrough is False

    session_config = server._build_session_config("You are Bob.")
    assert session_config.g711_passthrough is False
    assert session_config.input_format == "pcm16"


def test_greeting_unknown_caller_uses_configured_agent_name() -> None:
    """Verify the inbound greeting for unknown callers uses the configured agent name.

    Regression test for the trust bug where the model hallucinated names like
    'Gordon' or 'Alex' because the instructions said 'introduce yourself by name'
    without specifying what that name is.
    """
    greeting = _build_fresh_call_greeting_instructions("+1555000000", None, "bob")
    assert "Bob" in greeting
    assert "Gordon" not in greeting
    assert "Alex" not in greeting


def test_greeting_unknown_caller_capitalizes_agent_name() -> None:
    greeting = _build_fresh_call_greeting_instructions("+1555000000", None, "alice")
    assert "Alice" in greeting


def test_greeting_unknown_caller_default_name_is_bob() -> None:
    greeting = _build_fresh_call_greeting_instructions("+1555000000", None)
    assert "You are Bob" in greeting
    assert "Hello, this is Bob" in greeting


def test_server_uses_general_agent_name_for_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GPTME_VOICE_AGENT_NAME", raising=False)
    monkeypatch.setenv("GPTME_AGENT_NAME", "Alice Smith")

    server = VoiceServer()

    assert server._agent_name == "Alice Smith"
    assert server._instructions.startswith("IDENTITY: You are Alice smith.")


def test_server_general_display_name_does_not_change_handoff_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GPTME_VOICE_AGENT_NAME", raising=False)
    monkeypatch.setenv("GPTME_AGENT_NAME", "Alice Smith")
    monkeypatch.setenv("GPTME_VOICE_HANDOFF_DIR", str(tmp_path))
    monkeypatch.setenv("GPTME_VOICE_HANDOFF_SECRET", "test-secret")

    server = VoiceServer()

    assert server._agent_name == "Alice Smith"
    assert server._handoff_writer is not None
    assert server._handoff_writer.from_agent == "bob"
    assert "bob" not in server._available_agents


def test_server_voice_agent_name_overrides_general_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_NAME", "Bob")
    monkeypatch.setenv("GPTME_VOICE_AGENT_NAME", "Sven")

    server = VoiceServer()

    assert server._agent_name == "Sven"
    assert server._instructions.startswith("IDENTITY: You are Sven.")


# ---------------------------------------------------------------------------
# Voice activity digest tests
# ---------------------------------------------------------------------------


def test_load_voice_digest_returns_none_without_workspace() -> None:
    assert _load_voice_digest(None) is None


def test_load_voice_digest_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert _load_voice_digest(str(tmp_path)) is None


def test_load_voice_digest_returns_content_when_fresh(tmp_path: Path) -> None:
    digest_path = tmp_path / "state" / "voice-digest.md"
    digest_path.parent.mkdir(parents=True)
    digest_path.write_text("## Recent sessions\n- [10:00 UTC] shipped something\n")

    result = _load_voice_digest(str(tmp_path))

    assert result is not None
    assert "shipped something" in result


def test_load_voice_digest_returns_none_when_stale(tmp_path: Path) -> None:
    digest_path = tmp_path / "state" / "voice-digest.md"
    digest_path.parent.mkdir(parents=True)
    digest_path.write_text("## Recent sessions\n- [10:00 UTC] old work\n")
    # Make file appear old
    stale_mtime = time.time() - (_VOICE_DIGEST_MAX_AGE_SECONDS + 60)
    os.utime(digest_path, (stale_mtime, stale_mtime))

    assert _load_voice_digest(str(tmp_path)) is None


def test_load_voice_digest_returns_none_when_metadata_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest_path = tmp_path / "state" / "voice-digest.md"
    digest_path.parent.mkdir(parents=True)
    digest_path.write_text("## Recent sessions\n")
    original_stat = Path.stat

    def fail_digest_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == digest_path:
            raise OSError("digest disappeared")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_digest_stat)

    assert _load_voice_digest(str(tmp_path)) is None


def test_prepend_activity_digest_includes_guidance_and_content() -> None:
    digest = "## Recent sessions\n- [10:00 UTC] shipped heartbeat widget\n"
    instructions = "You are Bob."

    result = _prepend_activity_digest(digest, instructions)

    assert "ACTIVITY DIGEST" in result
    assert "shipped heartbeat widget" in result
    assert "You are Bob." in result
    # Guidance comes before instructions
    assert result.index("ACTIVITY DIGEST") < result.index("You are Bob.")


def test_prepend_activity_digest_tells_model_to_skip_subagent() -> None:
    result = _prepend_activity_digest("## Recent sessions\n", "instructions")
    assert "subagent" in result.lower()


# ── ASR partial deduplication ──────────────────────────────────────────────


def test_asr_partials_collapse_to_one_entry() -> None:
    """4 growing ASR partials for a single utterance must produce exactly 1 entry."""
    transcript: list[TranscriptTurn] = []
    partials = [
        "Hello Bob",
        "Hello Bob, what's up?",
        "Hello Bob, what's up? I wanted to brainstorm",
        "Hello Bob, what's up? I wanted to brainstorm some ideas.",
    ]
    for p in partials:
        _append_transcript_turn(transcript, "user", p)

    assert len(transcript) == 1
    assert transcript[0].text == partials[-1]


def test_asr_duplicate_partial_does_not_add_entry() -> None:
    """Sending the same text twice must not add a second entry."""
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "user", "Hello")
    _append_transcript_turn(transcript, "user", "Hello")
    assert len(transcript) == 1


def test_asr_new_utterance_after_role_switch_appends() -> None:
    """After an assistant turn, the next user turn must be a new entry."""
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "user", "Hello Bob")
    _append_transcript_turn(transcript, "assistant", "Hi there")
    _append_transcript_turn(transcript, "user", "What time is it?")

    assert len(transcript) == 3
    assert transcript[0] == TranscriptTurn(role="user", text="Hello Bob")
    assert transcript[1] == TranscriptTurn(role="assistant", text="Hi there")
    assert transcript[2] == TranscriptTurn(role="user", text="What time is it?")


def test_asr_shorter_text_starts_new_entry() -> None:
    """A user utterance shorter than the previous does not collapse into it."""
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "user", "Hello Bob, how are you doing today?")
    _append_transcript_turn(transcript, "user", "Goodbye")
    assert len(transcript) == 2


def test_asr_prefix_collision_with_item_id_appends() -> None:
    """A new utterance that starts with the previous text must NOT collapse when item_ids differ."""
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "user", "Hello", item_id="item-001")
    _append_transcript_turn(
        transcript, "user", "Hello, I'd like to order a pizza", item_id="item-002"
    )
    assert len(transcript) == 2
    assert transcript[0].text == "Hello"
    assert transcript[1].text == "Hello, I'd like to order a pizza"


def test_asr_same_item_id_replaces() -> None:
    """Multiple events with the same item_id replace rather than append."""
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "user", "Hello", item_id="item-001")
    _append_transcript_turn(transcript, "user", "Hello, I'd like", item_id="item-001")
    _append_transcript_turn(
        transcript, "user", "Hello, I'd like to order", item_id="item-001"
    )
    assert len(transcript) == 1
    assert transcript[0].text == "Hello, I'd like to order"
    assert transcript[0].item_id == "item-001"


def test_asr_same_item_id_shorter_text_is_ignored() -> None:
    """A shorter retransmission for the same item_id must not truncate stored text.

    Some providers retransmit a completed event for the same item_id with a
    corrected (shorter) transcript.  The stored longer text must be preserved;
    the shorter version is silently ignored rather than appended as a new entry
    or allowed to overwrite the longer partial.
    """
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(
        transcript, "user", "Hello, I'd like to order a pizza", item_id="item-001"
    )
    # Provider retransmits a shorter version for the same item — must be ignored.
    _append_transcript_turn(transcript, "user", "Hello, I'd", item_id="item-001")
    assert len(transcript) == 1, "shorter retransmission must not create a new entry"
    assert (
        transcript[0].text == "Hello, I'd like to order a pizza"
    ), "longer stored text must not be truncated"


def test_asr_prefix_collision_without_item_id_collapses() -> None:
    """Without item_id, a new utterance extending the previous text IS collapsed.

    This is the known false-positive risk of the prefix heuristic used when the
    provider does not expose item_id.  Providers that do send item_id (e.g. xAI)
    are immune because the item_id branch fires first and prevents false collapse.
    """
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "user", "Hello")
    _append_transcript_turn(transcript, "user", "Hello Bob, what's the plan?")
    # The second utterance starts with the first, so the heuristic collapses them.
    assert len(transcript) == 1
    assert transcript[0].text == "Hello Bob, what's the plan?"


def test_asr_no_id_event_does_not_replace_id_anchored_entry() -> None:
    """An event with item_id=None must NOT replace a stored entry that has an item_id.

    Mixed-provider scenario: the stored entry was anchored to item_id="item-001"
    by an earlier event.  A subsequent event arrives without an item_id (e.g. the
    provider stopped emitting it) but its text happens to start with the stored
    text.  The prefix heuristic must NOT fire here because the stored entry is
    already associated with a specific utterance — a no-id event is inherently
    ambiguous and should start a new entry.
    """
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "user", "Hello", item_id="item-001")
    # No item_id on the second call — text extends the first but comes from a
    # different (unknown) utterance; must append rather than replace.
    _append_transcript_turn(transcript, "user", "Hello Bob, what's the plan?")
    assert len(transcript) == 2
    assert transcript[0].text == "Hello"
    assert transcript[0].item_id == "item-001"
    assert transcript[1].text == "Hello Bob, what's the plan?"
    assert transcript[1].item_id is None


def test_assistant_turns_with_shared_prefix_are_not_collapsed() -> None:
    """Assistant final transcripts must NEVER be collapsed by the prefix heuristic.

    The deduplication logic exists for ASR partials (user role only).  If the
    assistant says "Sure" in one response and "Sure, let me check" in the next,
    the second must NOT replace the first — that would silently drop the earlier
    assistant turn from the conversation history used for resume context and
    post-call analysis.

    This is enforced by passing allow_continuation=False in _on_ai_transcript.
    """
    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "assistant", "Sure", allow_continuation=False)
    _append_transcript_turn(
        transcript, "assistant", "Sure, let me check", allow_continuation=False
    )
    assert (
        len(transcript) == 2
    ), "Two distinct assistant turns must remain separate even when the second starts with the first"
    assert transcript[0].text == "Sure"
    assert transcript[1].text == "Sure, let me check"
