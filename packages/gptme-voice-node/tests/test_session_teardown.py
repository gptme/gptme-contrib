"""Session teardown: disconnect mid-playback, in-flight audio I/O, unit file."""

import asyncio
import base64
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed


def _make_node():
    with patch("gptme_voice_node.node._get_pyaudio") as mock_get_pa:
        mod = MagicMock()
        mod.paInt16 = 8
        mod.PyAudio.return_value = MagicMock()
        mock_get_pa.return_value = mod
        from gptme_voice_node.node import VoiceNode

        return VoiceNode("ws://localhost:9999/local", "test-node")


@pytest.mark.asyncio
async def test_session_ends_when_server_drops_mid_playback():
    """Mic is muted while playing, so the send loop never sees the dead socket;
    the session must still end when the recv loop sees the disconnect."""
    node = _make_node()
    mic = MagicMock()

    def slow_read(*_a, **_k):
        time.sleep(0.01)
        return b"\x00\x00" * 1024

    mic.read.side_effect = slow_read
    node._pa.open.side_effect = [mic, MagicMock()]

    pcm = b"\x00\x01" * 512
    ws = AsyncMock()
    ws.recv.side_effect = [
        json.dumps({"type": "audio", "audio": base64.b64encode(pcm).decode()}),
        ConnectionClosed(None, None),
    ]
    ws.send.side_effect = ConnectionClosed(None, None)

    await asyncio.wait_for(node._session(ws), timeout=2)


@pytest.mark.asyncio
async def test_teardown_waits_for_inflight_mic_read_before_close():
    """PortAudio streams must not be stopped/closed under a live read()."""
    node = _make_node()
    in_read = threading.Event()
    reading = threading.Event()
    closed_during_read: list[tuple[str, bool]] = []

    def blocking_read(*_a, **_k):
        reading.set()
        in_read.set()
        time.sleep(0.3)  # a Pa_ReadStream still blocked when teardown starts
        reading.clear()
        return b"\x00\x00" * 1024

    mic = MagicMock()
    mic.read.side_effect = blocking_read
    mic.stop_stream.side_effect = lambda: closed_during_read.append(
        ("stop_stream", reading.is_set())
    )
    mic.close.side_effect = lambda: closed_during_read.append(
        ("close", reading.is_set())
    )
    node._pa.open.side_effect = [mic, MagicMock()]

    ws = AsyncMock()

    async def never_recv():
        await asyncio.sleep(30)

    ws.recv.side_effect = never_recv

    session = asyncio.create_task(node._session(ws))
    assert await asyncio.to_thread(in_read.wait, 1)
    session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(session, timeout=3)

    assert closed_during_read, "mic was never closed"
    assert not any(during for _, during in closed_during_read), closed_during_read


def test_unit_start_limit_is_in_unit_section():
    """systemd ignores StartLimitIntervalSec= under [Service]."""
    unit = (
        Path(__file__).parent.parent / "systemd" / "gptme-voice-node.service"
    ).read_text()
    section = None
    where: dict[str, str] = {}
    for line in unit.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif line.startswith("StartLimit"):
            where[line.split("=")[0]] = section or ""
    assert where == {"StartLimitIntervalSec": "Unit", "StartLimitBurst": "Unit"}
