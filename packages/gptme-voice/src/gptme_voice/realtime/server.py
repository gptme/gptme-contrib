"""
WebSocket server for Twilio Media Streams.

Bridges Twilio phone calls to a realtime API for real-time
voice conversations with gptme tool access.
"""

import base64
import json
import logging

import click
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
from .twilio_integration import (
    _get_config_env,
    build_connect_stream_twiml,
    build_stream_url,
)
from .xai_client import XAIRealtimeClient, _get_xai_api_key

logger = logging.getLogger(__name__)


def _build_caller_instructions(
    base_instructions: str, from_number: str, workspace: str | None
) -> str:
    """Prepend caller-identity context to session instructions.

    Looks up the caller's phone number in the workspace people/ directory to
    find a name.  Falls back to the raw phone number so the agent at least
    knows who is calling instead of being blind.
    """
    if not from_number:
        return base_instructions

    caller_name: str | None = None
    if workspace:
        from pathlib import Path

        people_dir = Path(workspace) / "people"
        if people_dir.is_dir():
            for md_file in people_dir.glob("*.md"):
                try:
                    text = md_file.read_text()
                    if from_number in text:
                        # Use the stem as a hint; the file header is the canonical name
                        first_h1 = next(
                            (
                                line.lstrip("# ").strip()
                                for line in text.splitlines()
                                if line.startswith("# ")
                            ),
                            None,
                        )
                        caller_name = first_h1 or md_file.stem.replace("-", " ").title()
                        break
                except Exception:
                    pass

    if caller_name:
        caller_ctx = (
            f"The current caller's phone number is {from_number} "
            f"({caller_name}). "
            f"You know this person — refer to them by name."
        )
    else:
        caller_ctx = (
            f"The current caller's phone number is {from_number}. "
            f"You do not recognise this number; treat the caller as an unknown guest."
        )

    return f"{caller_ctx}\n\n{base_instructions}"


_PROVIDER_OPENAI = "openai"
_PROVIDER_GROK = "grok"
_VALID_PROVIDERS = (_PROVIDER_OPENAI, _PROVIDER_GROK)


def _get_twilio_field(payload: dict, camel_name: str, snake_name: str) -> str | None:
    """Read Twilio fields, preferring the documented camelCase form."""
    return payload.get(camel_name) or payload.get(snake_name)


class VoiceServer:
    """
    WebSocket server that bridges Twilio Media Streams to a Realtime API.

    Supports OpenAI (default) and xAI Grok as providers.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        openai_api_key: str | None = None,
        workspace: str | None = None,
        provider: str = _PROVIDER_OPENAI,
        model: str | None = None,
    ):
        self.host = host
        self.port = port
        self.provider = provider
        self.model = model
        if provider == _PROVIDER_GROK:
            self._api_key = _get_xai_api_key()
        else:
            self._api_key = openai_api_key or _get_openai_api_key()
        self.workspace = workspace or _detect_agent_repo()
        self._instructions = _load_project_instructions(self.workspace)

        # Active connections: call_sid -> (twilio_ws, realtime_client)
        self._connections: dict[str, tuple] = {}

        # Create Starlette app
        self.app = Starlette(
            routes=[
                Route("/", self.health_check, methods=["GET"]),
                Route("/incoming", self.handle_incoming_call, methods=["POST"]),
                WebSocketRoute("/twilio", self.handle_twilio_websocket),
                WebSocketRoute("/local", self.handle_local_websocket),
            ]
        )

    async def health_check(self, request: Request) -> PlainTextResponse:
        """Health check endpoint."""
        return PlainTextResponse("OK")

    async def handle_incoming_call(self, request: Request) -> PlainTextResponse:
        """
        Handle incoming Twilio call — return TwiML to connect to Media Stream.

        Configure your Twilio phone number's Voice webhook to POST to this endpoint.
        Twilio will then open a Media Stream WebSocket to /twilio.
        """
        form_params = dict(await request.form())
        from_number = form_params.get("From", "")

        # Validate Twilio webhook signature when auth token is configured.
        # Skip in dev environments where TWILIO_AUTH_TOKEN is absent.
        auth_token = _get_config_env("TWILIO_AUTH_TOKEN")
        if auth_token:
            from twilio.request_validator import RequestValidator

            signature = request.headers.get("X-Twilio-Signature", "")
            host = request.headers.get("host", f"{self.host}:{self.port}")
            validation_url = f"https://{host}/incoming"
            if not RequestValidator(auth_token).validate(
                validation_url, form_params, signature
            ):
                logger.warning("Rejected request with invalid Twilio signature")
                return PlainTextResponse("Forbidden", status_code=403)

        # Allowlist: only accept calls from known numbers.
        # Set TWILIO_CALLER_ALLOWLIST to a comma-separated list of E.164 numbers.
        allowlist_raw = _get_config_env("TWILIO_CALLER_ALLOWLIST")
        if allowlist_raw:
            allowlist = {n.strip() for n in allowlist_raw.split(",") if n.strip()}
            if from_number not in allowlist:
                logger.warning(
                    "Rejected call from unlisted number: %s (allowlist: %s)",
                    from_number,
                    allowlist,
                )
                return PlainTextResponse("Forbidden", status_code=403)

        # Prefer the configured public URL; fall back to Host header.
        public_base_url = _get_config_env(
            "GPTME_VOICE_PUBLIC_BASE_URL"
        ) or _get_config_env("TWILIO_PUBLIC_BASE_URL")
        if public_base_url:
            ws_url = build_stream_url(public_base_url)
        else:
            host = request.headers.get("host", f"{self.host}:{self.port}")
            ws_url = build_stream_url(host)

        # Forward caller number to WebSocket handler via TwiML custom parameters.
        custom_params: dict[str, str] = {}
        if from_number:
            custom_params["from_number"] = from_number
        twiml = build_connect_stream_twiml(ws_url, custom_params or None)
        return PlainTextResponse(twiml, media_type="text/xml")

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
        realtime_client: OpenAIRealtimeClient | None = None
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
                    start = data.get("start", {})
                    stream_sid = _get_twilio_field(start, "streamSid", "stream_sid")
                    call_sid = _get_twilio_field(start, "callSid", "call_sid")
                    if not stream_sid:
                        logger.warning("Twilio start event missing streamSid: %s", data)
                        continue
                    if not call_sid:
                        call_sid = stream_sid

                    # Inject caller context into instructions (phone + name lookup)
                    custom_params = start.get("customParameters", {})
                    from_number = custom_params.get("from_number", "")
                    instructions = _build_caller_instructions(
                        self._instructions, from_number, self.workspace
                    )

                    if self.model:
                        session_cfg = SessionConfig(
                            instructions=instructions, model=self.model
                        )
                    else:
                        session_cfg = SessionConfig(instructions=instructions)
                    realtime_client = self._make_client(
                        session_cfg,
                        on_audio=lambda audio: self._send_to_twilio(
                            websocket,
                            stream_sid,
                            audio_converter.openai_to_twilio(audio),
                        ),
                    )
                    tool_bridge = GptmeToolBridge(
                        workspace=self.workspace,
                        on_result=realtime_client.inject_message,
                    )
                    realtime_client.on_function_call = tool_bridge.handle_function_call

                    await realtime_client.connect()
                    self._connections[call_sid] = (websocket, realtime_client)

                elif event == "media":
                    # Audio chunk from Twilio
                    if realtime_client:
                        # Extract μ-law audio
                        media = data.get("media", {})
                        mulaw_b64 = media.get("payload", "")
                        if mulaw_b64:
                            # Convert to PCM and send to realtime API
                            mulaw_data = base64.b64decode(mulaw_b64)
                            pcm_data = audio_converter.twilio_to_openai(mulaw_data)
                            await realtime_client.send_audio(pcm_data)

                elif event == "stop":
                    # Call ended
                    if realtime_client:
                        await realtime_client.disconnect()
                    if call_sid and call_sid in self._connections:
                        del self._connections[call_sid]
                    break

        except Exception as e:
            logger.exception("Error handling Twilio connection: %s", e)
        finally:
            if realtime_client:
                await realtime_client.disconnect()
            if call_sid and call_sid in self._connections:
                del self._connections[call_sid]

    async def _send_to_twilio(self, websocket, stream_sid: str, audio_data: bytes):
        """Send audio to Twilio Media Stream."""

        audio_b64 = base64.b64encode(audio_data).decode("utf-8")
        message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": audio_b64},
        }
        await websocket.send_text(json.dumps(message))

    def _make_client(
        self,
        session_config: SessionConfig,
        **kwargs,
    ) -> OpenAIRealtimeClient:
        """Instantiate the realtime client for the configured provider."""
        if self.provider == _PROVIDER_GROK:
            return XAIRealtimeClient(
                api_key=self._api_key,
                session_config=session_config,
                **kwargs,
            )
        return OpenAIRealtimeClient(
            api_key=self._api_key,
            session_config=session_config,
            **kwargs,
        )

    async def handle_local_websocket(self, websocket):
        """
        Handle WebSocket connection for local testing.

        Allows testing without Twilio by connecting directly from a browser
        or test client.
        """
        await websocket.accept()

        realtime_client: OpenAIRealtimeClient | None = None

        try:
            if self.model:
                session_cfg = SessionConfig(
                    instructions=self._instructions, model=self.model
                )
            else:
                session_cfg = SessionConfig(instructions=self._instructions)
            realtime_client = self._make_client(
                session_cfg,
                on_audio=lambda audio: self._send_local_audio(websocket, audio),
                on_audio_end=lambda: self._send_local_audio_end(websocket),
            )
            tool_bridge = GptmeToolBridge(
                workspace=self.workspace,
                on_result=realtime_client.inject_message,
            )
            realtime_client.on_function_call = tool_bridge.handle_function_call

            await realtime_client.connect()

            async for message in websocket.iter_text():
                data = json.loads(message)

                if data.get("type") == "audio":
                    # Audio chunk from client (PCM 24kHz)
                    audio_b64 = data.get("audio", "")
                    if audio_b64:
                        audio_data = base64.b64decode(audio_b64)
                        await realtime_client.send_audio(audio_data)

                elif data.get("type") == "commit":
                    await realtime_client.commit_audio()

        except Exception as e:
            logger.exception("Error handling local connection: %s", e)
        finally:
            if realtime_client:
                await realtime_client.disconnect()

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


@click.command()
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to bind to")
@click.option("--workspace", default=None, help="Working directory for gptme commands")
@click.option(
    "--provider",
    default=_PROVIDER_OPENAI,
    type=click.Choice(_VALID_PROVIDERS),
    show_default=True,
    help="Realtime API provider.",
)
@click.option(
    "--model",
    default=None,
    help=(
        "Override the realtime model. Useful for OpenAI; for xAI Grok, omit this "
        "unless you need a specific model alias from the xAI console."
    ),
)
@click.option("--debug", is_flag=True, help="Enable debug logging")
def main(
    host: str,
    port: int,
    workspace: str | None,
    provider: str,
    model: str | None,
    debug: bool,
):
    """Voice Interface Server for gptme."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy websockets debug logging (also leaks API key in headers)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    server = VoiceServer(
        host=host,
        port=port,
        workspace=workspace,
        provider=provider,
        model=model,
    )

    logger.info(f"Starting voice server on {host}:{port} (provider={provider})")
    logger.info(f"Local test endpoint: ws://{host}:{port}/local")

    server.run()


if __name__ == "__main__":
    main()
