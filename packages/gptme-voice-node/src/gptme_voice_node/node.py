"""
BobBrain voice node — thin WS client for embedded Linux.

Auto-reconnects to bob-voice-server with exponential backoff.
Low memory footprint, systemd-managed lifecycle.

Config (env vars):
  GPTME_VOICE_NODE_SERVER  — WebSocket URL of bob-voice-server (default: ws://localhost:8080/local)
  GPTME_VOICE_NODE_NAME    — Node identity string (default: bobbrain-unknown)

Protocol:
  Client → Server: {"type": "audio", "audio": "<base64 PCM 16-bit 24kHz mono>"}
  Client → Server: {"type": "commit"}      (optional flush signal)
  Server → Client: {"type": "audio", "audio": "<base64>"}
  Server → Client: {"type": "audio_end"}
"""

import asyncio
import base64
import functools
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlparse

try:
    import websockets
    from websockets.exceptions import ConnectionClosed, WebSocketException
except ImportError as e:
    raise ImportError(
        "websockets is required. Install with: pip install 'websockets>=12.0'"
    ) from e

# --- Config ---
SERVER_URL = os.environ.get("GPTME_VOICE_NODE_SERVER", "ws://localhost:8080/local")
NODE_NAME = os.environ.get("GPTME_VOICE_NODE_NAME", "bobbrain-unknown")

# --- Audio constants (must match server) ---
SAMPLE_RATE = 24000
CHUNK_SIZE = 1024
CHANNELS = 1

# Lazy import: pyaudio is only required when actually instantiating VoiceNode
_pyaudio = None


def _get_pyaudio():
    """Import pyaudio on demand, raising a helpful error if not installed."""
    global _pyaudio
    if _pyaudio is None:
        try:
            import pyaudio

            _pyaudio = pyaudio
        except ImportError as e:
            raise ImportError(
                "pyaudio is required. Install with: pip install 'gptme-voice-node[audio]'\n"
                "  On Debian/Ubuntu: sudo apt install portaudio19-dev python3-pyaudio"
            ) from e
    return _pyaudio


FORMAT = None  # Will be set to _get_pyaudio().paInt16 in VoiceNode.__init__

# Cooldown window (seconds) to keep mic muted after playback ends.
# Prevents echo/feedback on nodes without hardware AEC.
PLAYBACK_COOLDOWN = 0.5

# Reconnect backoff (seconds)
BACKOFF_INITIAL = 1
BACKOFF_MAX = 60
BACKOFF_RESET_AFTER = 30

log = logging.getLogger("gptme_voice_node")

T = TypeVar("T")


async def _run_audio_io(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run blocking audio I/O and let it finish before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _next_backoff(backoff: int, connected_at: float | None) -> int:
    """Reset after a healthy session; otherwise increase the retry delay."""
    if (
        connected_at is not None
        and time.monotonic() - connected_at >= BACKOFF_RESET_AFTER
    ):
        return BACKOFF_INITIAL
    return min(backoff * 2, BACKOFF_MAX)


class VoiceNode:
    """Thin embedded voice client.

    Manages one PyAudio instance across reconnects.  Audio streams are opened
    and closed per-session so the OS sees a clean release on disconnect.
    """

    def __init__(self, server_url: str, node_name: str) -> None:
        self.server_url = server_url
        self.node_name = node_name
        pyaudio = _get_pyaudio()  # May raise ImportError if not installed
        self._pa = pyaudio.PyAudio()
        self._audio_format = pyaudio.paInt16
        self._playing = False
        self._play_ended_at = 0.0
        self._running = True

    # --- Mic mute logic ---

    def _mic_muted(self) -> bool:
        """True when we should suppress outbound audio (echo prevention)."""
        return self._playing or (
            time.monotonic() - self._play_ended_at < PLAYBACK_COOLDOWN
        )

    # --- Per-session connect handler ---

    async def _session(self, ws) -> None:
        """Handle one connected WebSocket session."""
        # Reset per-session playback state on every reconnect.
        # Without this, a disconnect mid-playback leaves _playing=True, which
        # silently mutes the mic for the entire next session.
        self._playing = False
        self._play_ended_at = 0.0

        log.info("[%s] connected to %s", self.node_name, self.server_url)

        mic = self._pa.open(
            format=self._audio_format,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
            start=False,
        )
        try:
            speaker = self._pa.open(
                format=self._audio_format,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                output=True,
                frames_per_buffer=CHUNK_SIZE,
                start=False,
            )
            try:
                mic.start_stream()
                speaker.start_stream()
                await asyncio.gather(
                    self._send_loop(ws, mic),
                    self._recv_loop(ws, speaker),
                )
            finally:
                speaker.stop_stream()
                speaker.close()
        finally:
            mic.stop_stream()
            mic.close()

    async def _send_loop(self, ws, mic_stream) -> None:
        """Continuously read mic and forward to server (unless muted)."""
        while self._running:
            raw = await _run_audio_io(
                mic_stream.read, CHUNK_SIZE, exception_on_overflow=False
            )
            if not self._mic_muted():
                msg = {"type": "audio", "audio": base64.b64encode(raw).decode()}
                await ws.send(json.dumps(msg))

    async def _recv_loop(self, ws, speaker_stream) -> None:
        """Receive audio frames from server and play them.

        Uses explicit ws.recv() rather than ``async for`` so that the loop
        respects the ``_running`` flag and exits cleanly on shutdown instead
        of blocking indefinitely waiting for the next message.  This prevents
        run_forever from hanging and leaking PyAudio resources when the node
        is stopped via SIGTERM.
        """
        while self._running:
            try:
                raw_msg = await ws.recv()
            except ConnectionClosed:
                break
            data = json.loads(raw_msg)
            msg_type = data.get("type")
            if msg_type == "audio":
                self._playing = True
                pcm = base64.b64decode(data["audio"])
                await _run_audio_io(speaker_stream.write, pcm)
            elif msg_type == "audio_end":
                self._playing = False
                self._play_ended_at = time.monotonic()
                log.debug("[%s] audio_end received", self.node_name)

    # --- Reconnect loop ---

    async def run_forever(self) -> None:
        """Connect and auto-reconnect with exponential backoff."""
        backoff = BACKOFF_INITIAL
        while self._running:
            connected_at: float | None = None
            try:
                async with websockets.connect(
                    self.server_url,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    connected_at = time.monotonic()
                    await self._session(ws)
            except (ConnectionClosed, WebSocketException) as exc:
                log.warning(
                    "[%s] disconnected: %s — retrying in %ds",
                    self.node_name,
                    exc,
                    backoff,
                )
            except OSError as exc:
                log.warning(
                    "[%s] connection error: %s — retrying in %ds",
                    self.node_name,
                    exc,
                    backoff,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "[%s] unexpected error: %s — retrying in %ds",
                    self.node_name,
                    exc,
                    backoff,
                )
            if self._running:
                await asyncio.sleep(backoff)
                backoff = _next_backoff(backoff, connected_at)

    def stop(self) -> None:
        """Signal the node to shut down cleanly."""
        self._running = False

    def cleanup(self) -> None:
        """Release PyAudio resources."""
        self._pa.terminate()


async def _async_main(server_url: str, node_name: str) -> None:
    node = VoiceNode(server_url, node_name)

    loop = asyncio.get_event_loop()
    task = asyncio.ensure_future(node.run_forever())

    def _handle_signal(sig: signal.Signals) -> None:
        log.info("[%s] received signal %s, shutting down", node_name, sig)
        node.stop()
        # Cancel the task so any blocked ws.recv() unblocks immediately and
        # the cleanup finally block runs without waiting for SIGKILL.
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, functools.partial(_handle_signal, sig))

    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        node.cleanup()


def _warn_for_insecure_remote_url(server_url: str) -> None:
    """Warn when microphone audio would cross the network without TLS."""
    parsed = urlparse(server_url)
    if parsed.scheme == "ws" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        log.warning(
            "Microphone audio will be sent unencrypted to %s; use wss:// "
            "for remote deployments",
            parsed.hostname,
        )


def main() -> None:
    """Entry point for the gptme-voice-node CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )

    server_url = SERVER_URL
    node_name = NODE_NAME
    _warn_for_insecure_remote_url(server_url)

    log.info("Starting BobBrain voice node")
    log.info("  server : %s", server_url)
    log.info("  name   : %s", node_name)

    try:
        asyncio.run(_async_main(server_url, node_name))
    except KeyboardInterrupt:
        pass
    log.info("[%s] stopped", node_name)


if __name__ == "__main__":
    main()
