"""Tests for gptme-voice-node that don't require real audio hardware or network."""

import asyncio
import base64
import json
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


# ---------------------------------------------------------------------------
# Recv loop: audio frame handling
# ---------------------------------------------------------------------------


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
        # We expect write to be called with the PCM bytes
        written = []

        def fake_run_in_executor(_, fn):
            result = fn()
            written.append(result)
            fut = asyncio.get_event_loop().create_future()
            fut.set_result(None)
            return fut

        with patch.object(
            asyncio.get_event_loop(),
            "run_in_executor",
            side_effect=fake_run_in_executor,
        ):
            await node._recv_loop(mock_ws, speaker)

        assert node._playing is True  # set mid-loop; audio_end not sent
        assert speaker.write.called

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


# ---------------------------------------------------------------------------
# Stop / cleanup
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_stop_sets_running_false(self):
        node = _make_node()
        assert node._running is True
        node.stop()
        assert node._running is False

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
