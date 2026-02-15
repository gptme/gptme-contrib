"""
OpenAI Realtime API WebSocket client.

Handles bidirectional audio streaming and function calling for real-time
voice conversations.
"""

import asyncio
import base64
import json
import os
from typing import Callable, Optional, Any
from dataclasses import dataclass

import websockets  # type: ignore


@dataclass
class SessionConfig:
    """Configuration for OpenAI Realtime API session."""

    model: str = "gpt-4o-realtime-preview-2024-12-17"
    voice: str = "alloy"
    input_format: str = "pcm16"
    output_format: str = "pcm16"
    input_sample_rate: int = 24000
    output_sample_rate: int = 24000
    turn_detection: str = "server_vad"
    vad_threshold: float = 0.5


class OpenAIRealtimeClient:
    """
    WebSocket client for OpenAI Realtime API.

    Handles:
    - Bidirectional audio streaming
    - Function calling
    - Session management
    """

    WS_URL = "wss://api.openai.com/v1/realtime"

    def __init__(
        self,
        api_key: Optional[str] = None,
        session_config: Optional[SessionConfig] = None,
        on_audio: Optional[Callable[[bytes], None]] = None,
        on_transcript: Optional[Callable[[str], None]] = None,
        on_function_call: Optional[Callable[[str, dict], Any]] = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not found")

        self.session_config = session_config or SessionConfig()
        self.on_audio = on_audio
        self.on_transcript = on_transcript
        self.on_function_call = on_function_call

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        """Connect to OpenAI Realtime API."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        url = f"{self.WS_URL}?model={self.session_config.model}"
        self._ws = await websockets.connect(url, extra_headers=headers)

        # Configure session
        await self._send_event(
            "session.update",
            {
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": "You are a helpful assistant with access to tools via gptme.",
                    "voice": self.session_config.voice,
                    "input_audio_format": self.session_config.input_format,
                    "output_audio_format": self.session_config.output_format,
                    "input_audio_transcription": {"model": "whisper-1"},
                    "turn_detection": {
                        "type": self.session_config.turn_detection,
                        "threshold": self.session_config.vad_threshold,
                    },
                    "tools": [
                        {
                            "type": "function",
                            "name": "gptme_tool",
                            "description": "Execute a gptme tool or command",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "command": {
                                        "type": "string",
                                        "description": "The gptme command to execute",
                                    }
                                },
                                "required": ["command"],
                            },
                        }
                    ],
                }
            },
        )

        # Start receiving messages
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        """Disconnect from OpenAI Realtime API."""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send_audio(self, pcm_data: bytes) -> None:
        """
        Send audio to OpenAI Realtime API.

        Args:
            pcm_data: PCM audio data at 24kHz (16-bit signed little-endian)
        """
        audio_b64 = base64.b64encode(pcm_data).decode("utf-8")
        await self._send_event("input_audio_buffer.append", {"audio": audio_b64})

    async def commit_audio(self) -> None:
        """Commit the audio buffer for processing."""
        await self._send_event("input_audio_buffer.commit", {})

    async def _send_event(self, event_type: str, data: dict) -> None:
        """Send an event to the WebSocket."""
        if not self._ws:
            raise RuntimeError("Not connected to OpenAI Realtime API")

        event = {"type": event_type, **data}
        await self._ws.send(json.dumps(event))

    async def _receive_loop(self) -> None:
        """Receive and process messages from OpenAI."""
        if not self._ws:
            return

        try:
            async for message in self._ws:
                event = json.loads(message)
                await self._handle_event(event)
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise

    async def _handle_event(self, event: dict) -> None:
        """Handle an event from OpenAI Realtime API."""
        event_type = event.get("type", "")

        if event_type == "response.audio.delta":
            # Audio output chunk
            if self.on_audio:
                audio_b64 = event.get("delta", "")
                audio_data = base64.b64decode(audio_b64)
                await self._call_callback(self.on_audio, audio_data)

        elif event_type == "response.audio_transcript.delta":
            # Transcript of AI response
            if self.on_transcript:
                transcript = event.get("delta", "")
                await self._call_callback(self.on_transcript, transcript)

        elif event_type == "response.function_call":
            # Function call from AI
            if self.on_function_call:
                call_id = event.get("call_id", "")
                name = event.get("name", "")
                arguments = json.loads(event.get("arguments", "{}"))
                result = await self._call_callback(
                    self.on_function_call, name, arguments
                )
                # Send function result back
                await self._send_event(
                    "function_call.output",
                    {
                        "call_id": call_id,
                        "output": json.dumps(result) if result else "",
                    },
                )

    async def _call_callback(self, callback: Callable, *args) -> Any:
        """Call a callback, handling both sync and async functions."""
        import inspect

        result = callback(*args)
        if inspect.iscoroutine(result):
            result = await result
        return result
