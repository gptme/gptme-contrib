"""
WebSocket server for Twilio Media Streams.

Bridges Twilio phone calls to OpenAI Realtime API for real-time
voice conversations with gptme tool access.
"""

import base64
import json
import logging

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute

from .audio import AudioConverter
from .openai_client import (
    OpenAIRealtimeClient,
    SessionConfig,
    _detect_agent_repo,
    _get_openai_api_key,
    _load_project_instructions,
)
from .tool_bridge import GptmeToolBridge

logger = logging.getLogger(__name__)


class VoiceServer:
    """
    WebSocket server that bridges Twilio Media Streams to OpenAI Realtime API.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        openai_api_key: str | None = None,
        workspace: str | None = None,
    ):
        self.host = host
        self.port = port
        self.openai_api_key = openai_api_key or _get_openai_api_key()
        self.workspace = workspace or _detect_agent_repo()
        self._instructions = _load_project_instructions(self.workspace)

        # Active connections: call_sid -> (twilio_ws, openai_client)
        self._connections: dict[str, tuple] = {}

        # Create Starlette app
        self.app = Starlette(
            routes=[
                Route("/", self.health_check, methods=["GET"]),
                WebSocketRoute("/twilio", self.handle_twilio_websocket),
                WebSocketRoute("/local", self.handle_local_websocket),
            ]
        )

    async def health_check(self, request: Request) -> PlainTextResponse:
        """Health check endpoint."""
        return PlainTextResponse("OK")

    async def handle_twilio_websocket(self, websocket):
        """
        Handle WebSocket connection from Twilio Media Stream.

        Twilio sends:
        - "connected" event on connect
        - "start" event with call metadata
        - "media" events with μ-law audio chunks
        - "stop" event on call end
        """
        await websocket.accept()

        call_sid: str | None = None
        openai_client: OpenAIRealtimeClient | None = None
        audio_converter = AudioConverter()

        try:
            async for message in websocket.iter_text():
                data = json.loads(message)
                event = data.get("event")

                if event == "connected":
                    # Twilio connected, waiting for start
                    pass

                elif event == "start":
                    # Call started
                    call_sid = data.get("start", {}).get("call_sid")
                    stream_sid = data.get("start", {}).get("stream_sid")

                    # Create OpenAI client, then tool bridge with inject callback
                    openai_client = OpenAIRealtimeClient(
                        api_key=self.openai_api_key,
                        session_config=SessionConfig(instructions=self._instructions),
                        on_audio=lambda audio: self._send_to_twilio(  # type: ignore[arg-type]
                            websocket,
                            stream_sid,
                            audio_converter.openai_to_twilio(audio),
                        ),
                    )
                    tool_bridge = GptmeToolBridge(
                        workspace=self.workspace,
                        on_result=openai_client.inject_message,
                    )
                    openai_client.on_function_call = tool_bridge.handle_function_call

                    await openai_client.connect()
                    self._connections[call_sid] = (websocket, openai_client)  # type: ignore[index]

                elif event == "media":
                    # Audio chunk from Twilio
                    if openai_client:
                        # Extract μ-law audio
                        media = data.get("media", {})
                        mulaw_b64 = media.get("payload", "")
                        if mulaw_b64:
                            # Convert to PCM and send to OpenAI
                            mulaw_data = base64.b64decode(mulaw_b64)
                            pcm_data = audio_converter.twilio_to_openai(mulaw_data)
                            await openai_client.send_audio(pcm_data)

                elif event == "stop":
                    # Call ended
                    if openai_client:
                        await openai_client.disconnect()
                    if call_sid and call_sid in self._connections:
                        del self._connections[call_sid]
                    break

        except Exception as e:
            logger.exception("Error handling Twilio connection: %s", e)
        finally:
            if openai_client:
                await openai_client.disconnect()
            if call_sid and call_sid in self._connections:
                del self._connections[call_sid]

    async def _send_to_twilio(self, websocket, stream_sid: str, audio_data: bytes):
        """Send audio to Twilio Media Stream."""

        audio_b64 = base64.b64encode(audio_data).decode("utf-8")
        message = {
            "event": "media",
            "stream_sid": stream_sid,
            "media": {"payload": audio_b64},
        }
        await websocket.send_text(json.dumps(message))

    async def handle_local_websocket(self, websocket):
        """
        Handle WebSocket connection for local testing.

        Allows testing without Twilio by connecting directly from a browser
        or test client.
        """
        await websocket.accept()

        openai_client: OpenAIRealtimeClient | None = None

        try:
            # Create OpenAI client first, then tool bridge with inject callback
            openai_client = OpenAIRealtimeClient(
                api_key=self.openai_api_key,
                session_config=SessionConfig(instructions=self._instructions),
                on_audio=lambda audio: self._send_local_audio(websocket, audio),  # type: ignore[arg-type]
                on_audio_end=lambda: self._send_local_audio_end(websocket),
            )
            tool_bridge = GptmeToolBridge(
                workspace=self.workspace,
                on_result=openai_client.inject_message,
            )
            openai_client.on_function_call = tool_bridge.handle_function_call

            await openai_client.connect()

            async for message in websocket.iter_text():
                data = json.loads(message)

                if data.get("type") == "audio":
                    # Audio chunk from client (PCM 24kHz)
                    audio_b64 = data.get("audio", "")
                    if audio_b64:
                        audio_data = base64.b64decode(audio_b64)
                        await openai_client.send_audio(audio_data)

                elif data.get("type") == "commit":
                    await openai_client.commit_audio()

        except Exception as e:
            logger.exception("Error handling local connection: %s", e)
        finally:
            if openai_client:
                await openai_client.disconnect()

    async def _send_local_audio(self, websocket, audio_data: bytes):
        """Send audio to local client."""
        audio_b64 = base64.b64encode(audio_data).decode("utf-8")
        message = {"type": "audio", "audio": audio_b64}
        await websocket.send_text(json.dumps(message))

    async def _send_local_audio_end(self, websocket):
        """Signal to local client that audio response is complete."""
        message = {"type": "audio_end"}
        await websocket.send_text(json.dumps(message))

    def run(self):
        """Run the server."""
        uvicorn.run(self.app, host=self.host, port=self.port)


def main():
    """Entry point for running the voice server."""
    import argparse

    parser = argparse.ArgumentParser(description="Voice Interface Server for gptme")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument("--workspace", help="Working directory for gptme commands")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy websockets debug logging (also leaks API key in headers)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    server = VoiceServer(
        host=args.host,
        port=args.port,
        workspace=args.workspace,
    )

    logger.info(f"Starting voice server on {args.host}:{args.port}")
    logger.info(f"Local test endpoint: ws://{args.host}:{args.port}/local")

    server.run()


if __name__ == "__main__":
    main()
