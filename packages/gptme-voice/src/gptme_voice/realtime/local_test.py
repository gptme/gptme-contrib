#!/usr/bin/env python3
"""
Local testing mode for voice interface.

Allows testing the real-time voice interface without Twilio by using
the microphone and speaker directly.
"""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import pyaudio  # type: ignore

    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    print("Warning: pyaudio not available. Install with: pip install pyaudio")

import websockets  # type: ignore


class LocalVoiceTest:
    """
    Local test client for voice interface.

    Connects to the voice server via WebSocket and streams audio
    from microphone to speaker.

    Feedback loop prevention: mic input is suppressed while audio is
    playing and for a short cooldown after, to prevent speaker output
    from being picked up by the mic. This means you can't interrupt
    mid-sentence — use headphones if you need that.
    """

    SAMPLE_RATE = 24000
    CHUNK_SIZE = 1024
    CHANNELS = 1
    # How long after playback ends to keep mic muted (seconds)
    PLAYBACK_COOLDOWN = 0.5

    def __init__(self, server_url: str = "ws://localhost:8080/local"):
        self.server_url = server_url
        self.running = False
        self._playing = False
        self._play_ended_at: float = 0.0

        if PYAUDIO_AVAILABLE:
            self.audio = pyaudio.PyAudio()
        else:
            self.audio = None

    async def run(self):
        """Run the local voice test."""
        if not self.audio:
            print("Error: pyaudio not available")
            return

        print(f"Connecting to {self.server_url}...")
        print("Speak into your microphone. Press Ctrl+C to exit.\n")

        self.running = True

        try:
            async with websockets.connect(self.server_url) as ws:
                # Start audio tasks
                send_task = asyncio.create_task(self._send_audio(ws))
                receive_task = asyncio.create_task(self._receive_audio(ws))

                # Wait for either task to complete
                done, pending = await asyncio.wait(
                    [send_task, receive_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Cancel pending tasks
                for task in pending:
                    task.cancel()

        except Exception as e:
            print(f"Error: {e}")
        finally:
            self.running = False

    def _is_mic_muted(self) -> bool:
        """Check if mic should be muted (playing or in cooldown)."""
        if self._playing:
            return True
        if time.monotonic() - self._play_ended_at < self.PLAYBACK_COOLDOWN:
            return True
        return False

    async def _send_audio(self, ws):
        """Send audio from microphone to server."""
        stream = self.audio.open(
            format=pyaudio.paInt16,
            channels=self.CHANNELS,
            rate=self.SAMPLE_RATE,
            input=True,
            frames_per_buffer=self.CHUNK_SIZE,
        )

        try:
            while self.running:
                # Read audio chunk (always read to keep buffer drained)
                audio_data = stream.read(self.CHUNK_SIZE, exception_on_overflow=False)

                # Only send when not playing back to prevent feedback loops
                if not self._is_mic_muted():
                    message = {
                        "type": "audio",
                        "audio": base64.b64encode(audio_data).decode("utf-8"),
                    }
                    await ws.send(json.dumps(message))

                # Small delay to prevent overwhelming
                await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            pass
        finally:
            stream.stop_stream()
            stream.close()

    async def _receive_audio(self, ws):
        """Receive audio from server and play to speaker."""
        stream = self.audio.open(
            format=pyaudio.paInt16,
            channels=self.CHANNELS,
            rate=self.SAMPLE_RATE,
            output=True,
            frames_per_buffer=self.CHUNK_SIZE,
        )

        try:
            while self.running:
                # Receive message
                message = await ws.recv()
                data = json.loads(message)

                if data.get("type") == "audio":
                    # Decode and play
                    audio_b64 = data.get("audio", "")
                    if audio_b64:
                        self._playing = True
                        audio_data = base64.b64decode(audio_b64)
                        stream.write(audio_data)
                elif data.get("type") == "audio_end":
                    self._playing = False
                    self._play_ended_at = time.monotonic()

        except asyncio.CancelledError:
            pass
        finally:
            stream.stop_stream()
            stream.close()

    def cleanup(self):
        """Clean up audio resources."""
        if self.audio:
            self.audio.terminate()


async def _async_main():
    """Async entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Local voice interface test")
    parser.add_argument(
        "--server", default="ws://localhost:8080/local", help="Server WebSocket URL"
    )

    args = parser.parse_args()

    test = LocalVoiceTest(server_url=args.server)

    try:
        await test.run()
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        test.cleanup()


def main():
    """Synchronous entry point for console_scripts."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
