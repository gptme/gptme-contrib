"""Regression coverage for terminating a call only after farewell playback."""

import asyncio
from unittest.mock import Mock

import pytest
from gptme_voice.realtime import server as server_module
from gptme_voice.realtime.server import VoiceServer


def test_rest_termination_does_not_precede_farewell_wait(monkeypatch):
    """The former delay ran AFTER the authoritative REST hangup."""

    async def run():
        server = VoiceServer(openai_api_key="test")
        rest = Mock()
        monkeypatch.setattr("twilio.rest.Client", rest)
        monkeypatch.setattr(
            server_module,
            "_get_config_env",
            lambda key: "test" if key.startswith("TWILIO_") else None,
        )
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def farewell_wait(_seconds):
            waiting.set()
            await release.wait()

        monkeypatch.setattr(server_module.asyncio, "sleep", farewell_wait)
        ws = Mock()
        task = asyncio.create_task(
            server._schedule_hangup(
                ws, source="twilio:tool", reason="goodbye", call_sid="CA-test"
            )
        )
        try:
            await asyncio.wait_for(waiting.wait(), timeout=2)
            assert not rest.called, "Twilio terminated while farewell was still pending"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


class PlaybackSocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.closed = False

    async def send_text(self, message):
        import json

        await self.messages.put(json.loads(message))

    async def close(self):
        self.closed = True


async def next_mark(ws):
    message = await asyncio.wait_for(ws.messages.get(), timeout=1)
    assert message["event"] == "mark"
    assert message["streamSid"] == "MZ-test"
    return message["mark"]["name"]


def test_generation_and_matching_playback_ack_both_required():
    from gptme_voice.realtime.playback import TwilioPlayback

    async def run():
        ws = PlaybackSocket()
        playback = TwilioPlayback(ws, "MZ-test")
        playback.response_started()
        drain = asyncio.create_task(playback.wait_until_played())
        await asyncio.sleep(0)
        assert ws.messages.empty()
        playback.audio_sent()
        playback.response_finished()
        mark = await next_mark(ws)
        assert not drain.done()
        playback.acknowledge("unrelated-mark")
        await asyncio.sleep(0)
        assert not drain.done()
        playback.acknowledge(mark)
        await asyncio.wait_for(drain, timeout=1)

    asyncio.run(run())


def test_audio_queued_after_mark_requires_another_mark():
    from gptme_voice.realtime.playback import TwilioPlayback

    async def run():
        ws = PlaybackSocket()
        playback = TwilioPlayback(ws, "MZ-test")
        playback.audio_sent()
        drain = asyncio.create_task(playback.wait_until_played())
        first = await next_mark(ws)
        playback.response_started()
        playback.audio_sent()
        playback.acknowledge(first)
        await asyncio.sleep(0)
        assert not drain.done()
        assert ws.messages.empty()
        playback.response_finished()
        second = await next_mark(ws)
        assert first != second
        playback.acknowledge(first)
        await asyncio.sleep(0)
        assert not drain.done()
        playback.acknowledge(second)
        await asyncio.wait_for(drain, timeout=1)

    asyncio.run(run())


def test_speech_cancels_tool_and_late_transcript_fallback(monkeypatch):
    from gptme_voice.realtime.playback import TwilioPlayback
    from gptme_voice.realtime.server import TranscriptTurn
    from gptme_voice.realtime.tool_bridge import GptmeToolBridge

    async def run():
        ws = PlaybackSocket()
        server = VoiceServer(openai_api_key="test")
        rest = Mock()
        monkeypatch.setattr("twilio.rest.Client", rest)
        monkeypatch.setattr(server_module, "_get_config_env", lambda key: "test")
        playback = TwilioPlayback(ws, "MZ-test")
        server._authenticated_stream_calls.add("CA-test")
        on_ai, _, on_hangup = server._make_transcript_callbacks(
            transcript=[TranscriptTurn(role="user", text="Goodbye")],
            websocket=ws,
            source="twilio",
            call_sid="CA-test",
            playback=playback,
        )
        bridge = GptmeToolBridge(workspace="/fake/workspace", on_hangup=on_hangup)
        playback.response_started()
        result = await bridge.handle_function_call("hangup", {})
        assert result["status"] == "hanging_up"
        # No yield here: the scheduling callback must already have run.
        pending = playback.hangup_task
        assert pending is not None
        playback.cancel_hangup()
        await on_ai("I'll hang up now.")
        playback.response_finished()
        await asyncio.gather(pending, return_exceptions=True)
        assert pending.cancelled()
        assert playback.hangup_task is pending
        assert not rest.called
        assert not ws.closed
        assert "CA-test" in server._authenticated_stream_calls
        # A later, genuine farewell remains usable.
        playback.response_started()
        await on_hangup("goodbye again")
        playback.response_finished()
        mark = await next_mark(ws)
        playback.acknowledge(mark)
        await asyncio.wait_for(playback.hangup_task, timeout=1)
        rest.return_value.calls.return_value.update.assert_called_once_with(
            status="completed"
        )
        assert ws.closed
        assert "CA-test" not in server._authenticated_stream_calls

    asyncio.run(run())


@pytest.mark.parametrize("missing_event", ["response.done", "mark"])
def test_playback_timeout_preserves_termination(monkeypatch, caplog, missing_event):
    from gptme_voice.realtime import playback as playback_module

    async def run():
        monkeypatch.setattr(playback_module, "HANGUP_DRAIN_TIMEOUT_SECONDS", 0.01)
        ws = PlaybackSocket()
        playback = playback_module.TwilioPlayback(ws, "MZ-test")
        if missing_event == "response.done":
            playback.response_started()
        server = VoiceServer(openai_api_key="test")
        monkeypatch.setattr(server_module, "_get_config_env", lambda key: None)
        await asyncio.wait_for(
            server._schedule_hangup(
                ws,
                source="twilio:tool",
                reason="bye",
                call_sid="CA-test",
                playback=playback,
            ),
            timeout=1,
        )
        assert ws.closed
        assert "playback unconfirmed" in caplog.text

    asyncio.run(run())


def test_hangup_receipt_does_not_generate_another_response():
    from gptme_voice.realtime.openai_client import OpenAIRealtimeClient

    assert not OpenAIRealtimeClient._should_auto_respond_after_function_output(
        "hangup", {"status": "hanging_up"}
    )
    assert OpenAIRealtimeClient._should_auto_respond_after_function_output(
        "hangup", {"status": "not_supported"}
    )


def test_rest_failure_still_closes_websocket_after_playback(monkeypatch):
    from gptme_voice.realtime.playback import TwilioPlayback

    async def run():
        server = VoiceServer(openai_api_key="test")
        rest = Mock()
        rest.return_value.calls.return_value.update.side_effect = RuntimeError(
            "offline"
        )
        monkeypatch.setattr("twilio.rest.Client", rest)
        monkeypatch.setattr(server_module, "_get_config_env", lambda key: "test")
        ws = PlaybackSocket()
        playback = TwilioPlayback(ws, "MZ-test")
        task = asyncio.create_task(
            server._schedule_hangup(
                ws,
                source="twilio:tool",
                reason="bye",
                call_sid="CA-test",
                playback=playback,
            )
        )
        mark = await next_mark(ws)
        assert not rest.called
        playback.acknowledge(mark)
        await asyncio.wait_for(task, timeout=1)
        assert ws.closed
        rest.return_value.calls.return_value.update.assert_called_once_with(
            status="completed"
        )

    asyncio.run(run())


def test_disconnect_during_drain_still_completes_call(monkeypatch):
    """A dropped transport must not skip the authoritative REST kill."""

    from gptme_voice.realtime.playback import TwilioPlayback

    async def run():
        server = VoiceServer(openai_api_key="test")
        rest = Mock()
        monkeypatch.setattr("twilio.rest.Client", rest)
        monkeypatch.setattr(server_module, "_get_config_env", lambda key: "test")
        ws = PlaybackSocket()
        playback = TwilioPlayback(ws, "MZ-test")
        task = asyncio.create_task(
            server._schedule_hangup(
                ws,
                source="twilio:tool",
                reason="bye",
                call_sid="CA-test",
                playback=playback,
            )
        )
        await next_mark(ws)
        # The websocket handler's finally block, on a mid-drain disconnect.
        playback.stopped = True
        playback.disconnected = True
        playback.cancel_hangup()
        playback.hangup_task = task
        task.cancel()
        task.cancel()
        await asyncio.wait_for(task, timeout=1)
        rest.return_value.calls.return_value.update.assert_called_once_with(
            status="completed"
        )

    asyncio.run(run())


def test_response_callbacks_preserve_legacy_positional_transcript_arguments():
    from gptme_voice.realtime.openai_client import OpenAIRealtimeClient

    transcript_callback = Mock()
    client = OpenAIRealtimeClient("test", None, None, None, transcript_callback)
    assert client.on_transcript is transcript_callback
    assert client.on_response_start is None
    assert client.on_response_end is None
