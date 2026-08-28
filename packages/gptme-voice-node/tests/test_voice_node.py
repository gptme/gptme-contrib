"""Tests for gptme-voice-node that don't require real audio hardware or network."""

import asyncio
import base64
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

# ---------------------------------------------------------------------------
# Helpers to avoid pyaudio import in test collection
# ---------------------------------------------------------------------------


def _make_node(server_url="ws://localhost:9999/local", node_name="test-node"):
    """Create a VoiceNode with pyaudio mocked out."""
    with patch("gptme_voice_node.node._get_pyaudio") as mock_get_pa:
        mock_pa_module = MagicMock()
        mock_pa_module.paInt16 = 8  # arbitrary constant
        mock_pa_module.PyAudio.return_value = MagicMock()
        mock_get_pa.return_value = mock_pa_module
        from gptme_voice_node.node import VoiceNode

        node = VoiceNode(server_url, node_name)
    return node


# ---------------------------------------------------------------------------
# Mic mute logic
# ---------------------------------------------------------------------------


class TestMicMute:
    def test_not_muted_by_default(self):
        node = _make_node()
        assert not node._mic_muted()

    def test_muted_during_playback(self):
        node = _make_node()
        node._playing = True
        assert node._mic_muted()

    def test_muted_in_cooldown_window(self):
        node = _make_node()
        node._playing = False
        node._play_ended_at = time.monotonic()  # just now
        assert node._mic_muted()

    def test_unmuted_after_cooldown(self):
        node = _make_node()
        node._playing = False
        node._play_ended_at = time.monotonic() - 10  # 10 seconds ago
        assert not node._mic_muted()

    def test_not_muted_at_early_monotonic_time(self):
        node = _make_node()
        with patch("gptme_voice_node.node.time.monotonic", return_value=0.1):
            assert not node._mic_muted()


# ---------------------------------------------------------------------------
# Send/recv loop handling
# ---------------------------------------------------------------------------


class TestSendLoop:
    @pytest.mark.asyncio
    async def test_audio_io_cancellation_does_not_wait_for_inflight_thread(self):
        from gptme_voice_node.node import _run_audio_io

        thread_started = threading.Event()
        allow_thread_to_finish = threading.Event()

        def operation():
            thread_started.set()
            allow_thread_to_finish.wait(timeout=1)
            return b"audio"

        task = asyncio.create_task(_run_audio_io(operation))
        try:
            assert await asyncio.to_thread(thread_started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.1)
        finally:
            allow_thread_to_finish.set()


class TestRecvLoop:
    @pytest.mark.asyncio
    async def test_audio_frame_triggers_playback_flag(self):
        node = _make_node()
        node._playing = False

        pcm = b"\x00\x01" * 512
        msg = json.dumps({"type": "audio", "audio": base64.b64encode(pcm).decode()})

        mock_ws = AsyncMock()
        # Return the audio frame then ConnectionClosed to exit the recv loop.
        mock_ws.recv.side_effect = [msg, ConnectionClosed(None, None)]

        speaker = MagicMock()

        async def fake_audio_io(function, *args, **kwargs):
            return function(*args, **kwargs)

        with patch("gptme_voice_node.node._run_audio_io", fake_audio_io):
            await node._recv_loop(mock_ws, speaker)

        assert node._playing is True  # set mid-loop; audio_end not sent
        speaker.write.assert_called_once_with(pcm)

    @pytest.mark.asyncio
    async def test_audio_end_clears_playing_flag(self):
        node = _make_node()
        node._playing = True

        mock_ws = AsyncMock()
        # Return audio_end then ConnectionClosed to exit the recv loop.
        mock_ws.recv.side_effect = [
            json.dumps({"type": "audio_end"}),
            ConnectionClosed(None, None),
        ]
        speaker = MagicMock()

        await node._recv_loop(mock_ws, speaker)

        assert node._playing is False
        assert node._play_ended_at > 0

    @pytest.mark.asyncio
    async def test_spurious_audio_end_does_not_start_cooldown(self):
        node = _make_node()
        node._playing = False

        mock_ws = AsyncMock()
        mock_ws.recv.side_effect = [
            json.dumps({"type": "audio_end"}),
            ConnectionClosed(None, None),
        ]

        await node._recv_loop(mock_ws, MagicMock())

        assert node._play_ended_at == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            "not json",
            json.dumps(["not", "an object"]),
            json.dumps({"type": "audio"}),
            json.dumps({"type": "audio", "audio": "not base64!"}),
        ],
    )
    async def test_malformed_message_is_ignored(self, message, caplog):
        node = _make_node()
        mock_ws = AsyncMock()
        mock_ws.recv.side_effect = [message, ConnectionClosed(None, None)]
        speaker = MagicMock()

        await node._recv_loop(mock_ws, speaker)

        speaker.write.assert_not_called()
        assert "ignoring malformed message" in caplog.text


# ---------------------------------------------------------------------------
# Transport safety warning
# ---------------------------------------------------------------------------


class TestTransportWarning:
    def test_url_userinfo_is_redacted(self):
        from gptme_voice_node.node import _redact_url_userinfo

        redacted = _redact_url_userinfo("wss://user:secret@example.com:8443/local")

        assert redacted == "wss://***@example.com:8443/local"
        assert "user" not in redacted
        assert "secret" not in redacted

    def test_warns_for_insecure_remote_url(self, caplog):
        from gptme_voice_node.node import _warn_for_insecure_remote_url

        _warn_for_insecure_remote_url("ws://bob-host.local:8080/local")

        assert "sent unencrypted" in caplog.text

    def test_local_ws_url_does_not_warn(self, caplog):
        from gptme_voice_node.node import _warn_for_insecure_remote_url

        _warn_for_insecure_remote_url("ws://localhost:8080/local")

        assert not caplog.text

    @pytest.mark.asyncio
    async def test_session_connect_log_redacts_userinfo(self, caplog):
        caplog.set_level("INFO")
        node = _make_node("wss://user:secret@example.com:8443/local")
        mic = MagicMock()
        speaker = MagicMock()
        node._pa.open.side_effect = [mic, speaker]

        async def stop_session(*_args):
            node.stop()

        with (
            patch.object(node, "_send_loop", AsyncMock(side_effect=stop_session)),
            patch.object(node, "_recv_loop", AsyncMock(side_effect=stop_session)),
        ):
            await node._session(AsyncMock())

        assert "secret" not in caplog.text
        assert "wss://***@example.com:8443/local" in caplog.text

    @pytest.mark.asyncio
    async def test_disconnect_log_redacts_userinfo(self, caplog):
        url = "wss://user:secret@example.com:8443/local"
        node = _make_node(url)

        async def stop_on_sleep(_delay):
            node.stop()

        with (
            patch(
                "gptme_voice_node.node.websockets.connect",
                side_effect=OSError(f"failed to connect to {url}"),
            ),
            patch("gptme_voice_node.node.asyncio.sleep", side_effect=stop_on_sleep),
        ):
            await node.run_forever()

        assert "secret" not in caplog.text
        assert "user:secret" not in caplog.text
        assert "wss://***@example.com:8443/local" in caplog.text


# ---------------------------------------------------------------------------
# Stop / cleanup
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_stop_sets_running_false(self):
        node = _make_node()
        assert node._running is True
        node.stop()
        assert node._running is False

    @pytest.mark.asyncio
    async def test_session_explicitly_starts_audio_streams(self):
        node = _make_node()
        mic = MagicMock()
        speaker = MagicMock()
        node._pa.open.side_effect = [mic, speaker]

        async def stop_session(*_args):
            node.stop()

        with (
            patch.object(node, "_send_loop", AsyncMock(side_effect=stop_session)),
            patch.object(node, "_recv_loop", AsyncMock(side_effect=stop_session)),
        ):
            await node._session(AsyncMock())

        mic.start_stream.assert_called_once()
        speaker.start_stream.assert_called_once()
        assert node._pa.open.call_args_list[0].kwargs["start"] is False
        assert node._pa.open.call_args_list[1].kwargs["start"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failing_stream", ["mic", "speaker"])
    async def test_session_closes_streams_when_start_fails(self, failing_stream):
        node = _make_node()
        mic = MagicMock()
        speaker = MagicMock()
        node._pa.open.side_effect = [mic, speaker]
        stream = mic if failing_stream == "mic" else speaker
        stream.start_stream.side_effect = OSError("device unavailable")

        with pytest.raises(OSError, match="device unavailable"):
            await node._session(AsyncMock())

        mic.close.assert_called_once()
        speaker.close.assert_called_once()
        stream.stop_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_cancels_sibling_when_one_loop_fails(self):
        node = _make_node()
        mic = MagicMock()
        speaker = MagicMock()
        node._pa.open.side_effect = [mic, speaker]
        recv_started = asyncio.Event()
        recv_cancelled = asyncio.Event()

        async def send_loop(*_args):
            await recv_started.wait()
            raise OSError("mic dead")

        async def recv_loop(*_args):
            recv_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                recv_cancelled.set()
                raise

        with (
            patch.object(node, "_send_loop", send_loop),
            patch.object(node, "_recv_loop", recv_loop),
        ):
            with pytest.raises(OSError, match="mic dead"):
                await asyncio.wait_for(node._session(AsyncMock()), timeout=1)

        assert recv_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_session_attempts_both_closes_when_one_close_fails(self, caplog):
        node = _make_node()
        mic = MagicMock()
        speaker = MagicMock()
        node._pa.open.side_effect = [mic, speaker]
        speaker.close.side_effect = OSError("device gone")
        node.stop()

        await node._session(AsyncMock())

        mic.close.assert_called_once()
        speaker.close.assert_called_once()
        assert "failed to close audio stream" in caplog.text

    def test_cleanup_terminates_pyaudio(self):
        with patch("gptme_voice_node.node._get_pyaudio") as mock_get_pa:
            mock_pa_module = MagicMock()
            mock_pa_module.paInt16 = 8
            mock_pa = MagicMock()
            mock_pa_module.PyAudio.return_value = mock_pa
            mock_get_pa.return_value = mock_pa_module
            from gptme_voice_node.node import VoiceNode

            node = VoiceNode("ws://x", "test")
            node.cleanup()
            mock_pa.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# Reconnect backoff behaviour (state machine only)
# ---------------------------------------------------------------------------


class TestBackoff:
    @pytest.mark.asyncio
    async def test_run_forever_stops_when_not_running(self):
        """If _running is already False, run_forever exits without connecting."""
        node = _make_node()
        node._running = False

        with patch("gptme_voice_node.node.websockets") as mock_ws:
            mock_ws.connect = AsyncMock()
            await node.run_forever()
            mock_ws.connect.assert_not_called()

    def test_quick_session_failure_increases_exponential_backoff(self):
        from gptme_voice_node.node import _next_backoff

        with patch("gptme_voice_node.node.time.monotonic", return_value=100.0):
            assert _next_backoff(8, connected_at=99.0) == 16

    def test_stable_session_resets_exponential_backoff(self):
        from gptme_voice_node.node import BACKOFF_INITIAL, _next_backoff

        with patch("gptme_voice_node.node.time.monotonic", return_value=100.0):
            assert _next_backoff(8, connected_at=60.0) == BACKOFF_INITIAL

    @pytest.mark.asyncio
    async def test_retry_log_matches_sleep_delay(self, caplog):
        node = _make_node()

        async def stop_on_sleep(delay):
            assert delay == 2
            node.stop()

        with (
            patch(
                "gptme_voice_node.node.websockets.connect",
                side_effect=OSError("offline"),
            ),
            patch("gptme_voice_node.node.asyncio.sleep", side_effect=stop_on_sleep),
        ):
            await node.run_forever()

        assert "retrying in 2s" in caplog.text

    @pytest.mark.asyncio
    async def test_stable_session_uses_reset_delay_for_immediate_retry(self):
        from gptme_voice_node.node import BACKOFF_INITIAL

        node = _make_node()

        class Connection:
            async def __aenter__(self):
                return AsyncMock()

            async def __aexit__(self, *_args):
                return False

        async def stop_on_sleep(delay):
            assert delay == BACKOFF_INITIAL
            node.stop()

        with (
            patch(
                "gptme_voice_node.node.websockets.connect", return_value=Connection()
            ),
            patch.object(node, "_session", new=AsyncMock()),
            patch("gptme_voice_node.node._next_backoff", return_value=BACKOFF_INITIAL),
            patch("gptme_voice_node.node.asyncio.sleep", side_effect=stop_on_sleep),
        ):
            await node.run_forever()
