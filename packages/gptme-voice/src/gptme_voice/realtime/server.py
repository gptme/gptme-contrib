"""
WebSocket server for Twilio Media Streams.

Bridges Twilio phone calls to a realtime API for real-time
voice conversations with gptme tool access.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import shlex
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import click
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

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

_DEFAULT_RESUME_WINDOW_SECONDS = 300
_DEFAULT_STATE_DIR = "/tmp/gptme-voice-call-state"
_MAX_RESUME_TRANSCRIPT_CHARS = 2500

# Delay before actually closing the WebSocket after the model requests hangup,
# so the goodbye utterance has time to reach the caller.
_HANGUP_FAREWELL_DELAY_SECONDS = 5.0
_CALL_END_DRAIN_TIMEOUT_SECONDS = 1.5
_CALL_END_IDLE_TIMEOUT_SECONDS = 0.25


@dataclass
class TranscriptTurn:
    role: str
    text: str


@dataclass
class RecentCallRecord:
    caller_id: str
    source: str
    ended_at: float
    transcript: list[TranscriptTurn]
    metadata: dict[str, str]
    subagent_timings: list[dict[str, object]] = field(default_factory=list)


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


def _append_transcript_turn(
    transcript: list[TranscriptTurn], role: str, text: str
) -> None:
    """Append a cleaned turn to the transcript if it contains useful text."""
    cleaned = text.strip()
    if cleaned:
        transcript.append(TranscriptTurn(role=role, text=cleaned))


def _format_transcript(transcript: list[TranscriptTurn]) -> str:
    return "\n".join(f"{turn.role.title()}: {turn.text}" for turn in transcript)


def _truncate_resume_transcript(transcript_text: str, max_chars: int) -> str:
    """Keep the newest transcript lines without starting mid-line."""
    if len(transcript_text) <= max_chars:
        return transcript_text

    lines = transcript_text.splitlines()
    kept_lines: list[str] = []
    total_chars = 0

    for line in reversed(lines):
        line_chars = len(line) + (1 if kept_lines else 0)
        if kept_lines and total_chars + line_chars > max_chars:
            break
        if not kept_lines and len(line) > max_chars:
            return line[-max_chars:]

        kept_lines.append(line)
        total_chars += line_chars

    if kept_lines:
        return "\n".join(reversed(kept_lines))

    return transcript_text[-max_chars:]


def _build_resume_instructions(
    base_instructions: str,
    recent_call: RecentCallRecord | None,
    resume_window_seconds: int,
) -> str:
    """Prepend recent-call context when a caller reconnects quickly."""
    if not recent_call or not recent_call.transcript:
        return base_instructions

    transcript_text = _format_transcript(recent_call.transcript)
    transcript_text = _truncate_resume_transcript(
        transcript_text, _MAX_RESUME_TRANSCRIPT_CHARS
    )

    age_seconds = max(int(time.time() - recent_call.ended_at), 0)
    resume_ctx = (
        "The current caller reconnected after a brief disconnect. "
        f"This prior call ended {age_seconds} seconds ago, within the "
        f"{resume_window_seconds}-second resume window. "
        "Continue naturally from the previous conversation instead of starting over.\n\n"
        f"Previous transcript:\n{transcript_text}"
    )
    return f"{resume_ctx}\n\n{base_instructions}"


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
        self.resume_window_seconds = int(
            _get_config_env("GPTME_VOICE_RESUME_WINDOW_SECONDS")
            or _DEFAULT_RESUME_WINDOW_SECONDS
        )
        self.post_call_delay_seconds = int(
            _get_config_env("GPTME_VOICE_POST_CALL_DELAY_SECONDS")
            or self.resume_window_seconds
        )
        self.post_call_command = _get_config_env("GPTME_VOICE_POST_CALL_COMMAND")
        self.state_dir = Path(
            _get_config_env("GPTME_VOICE_STATE_DIR") or _DEFAULT_STATE_DIR
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Active connections: call_sid -> (twilio_ws, realtime_client)
        self._connections: dict[str, tuple] = {}
        self._pending_post_calls: dict[str, asyncio.Task[None]] = {}

        # Create Starlette app
        self.app = Starlette(
            routes=[
                Route("/", self.health_check, methods=["GET"]),
                Route("/incoming", self.handle_incoming_call, methods=["POST"]),
                WebSocketRoute("/twilio", self.handle_twilio_websocket),
                WebSocketRoute("/local", self.handle_local_websocket),
            ]
        )

    def _recent_call_path(self, caller_id: str) -> Path:
        digest = hashlib.sha256(caller_id.encode("utf-8")).hexdigest()[:16]
        return self._recent_state_dir() / f"{digest}.json"

    def _legacy_recent_call_path(self, caller_id: str) -> Path:
        digest = hashlib.sha256(caller_id.encode("utf-8")).hexdigest()[:16]
        return self.state_dir / f"{digest}.json"

    def _recent_state_dir(self) -> Path:
        return self.state_dir / "recent"

    def _handoff_state_dir(self) -> Path:
        return self.state_dir / "handoffs"

    def _call_archive_dir(self) -> Path:
        return self.state_dir / "archive"

    def _handoff_bootstrap_path(self, handoff_id: str) -> Path:
        safe_handoff_id = "".join(
            ch for ch in handoff_id if ch.isalnum() or ch in {"-", "_"}
        )
        if not safe_handoff_id:
            safe_handoff_id = "handoff"
        return self._handoff_state_dir() / f"{safe_handoff_id}.json"

    def _call_record_path(self, record: RecentCallRecord) -> Path:
        identifier = (
            record.metadata.get("call_sid")
            or record.metadata.get("stream_sid")
            or hashlib.sha256(
                f"{record.caller_id}:{record.ended_at}:{record.source}".encode()
            ).hexdigest()[:16]
        )
        safe_identifier = "".join(
            ch for ch in identifier if ch.isalnum() or ch in {"-", "_"}
        )
        if not safe_identifier:
            safe_identifier = "call"
        ended_at = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(record.ended_at))
        milliseconds = int((record.ended_at % 1) * 1000)
        return (
            self._call_archive_dir()
            / f"{ended_at}-{milliseconds:03d}-{record.source}-{safe_identifier}.json"
        )

    def _record_payload(self, record: RecentCallRecord) -> dict[str, object]:
        payload: dict[str, object] = {
            "caller_id": record.caller_id,
            "source": record.source,
            "ended_at": record.ended_at,
            "transcript": [asdict(turn) for turn in record.transcript],
            "metadata": record.metadata,
        }
        if record.subagent_timings:
            payload["subagent_timings"] = record.subagent_timings
        return payload

    def _write_call_record(self, path: Path, record: RecentCallRecord) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._record_payload(record), indent=2, sort_keys=True)
        )
        return path

    def _save_recent_call(self, record: RecentCallRecord) -> Path:
        return self._write_call_record(self._recent_call_path(record.caller_id), record)

    def _save_call_record(self, record: RecentCallRecord) -> Path:
        return self._write_call_record(self._call_record_path(record), record)

    def _load_recent_call(self, caller_id: str) -> RecentCallRecord | None:
        for path in (
            self._recent_call_path(caller_id),
            self._legacy_recent_call_path(caller_id),
        ):
            if not path.exists():
                continue

            try:
                payload = json.loads(path.read_text())
                transcript = [
                    TranscriptTurn(role=item["role"], text=item["text"])
                    for item in payload.get("transcript", [])
                    if item.get("role") and item.get("text")
                ]
                raw_timings = payload.get("subagent_timings") or []
                subagent_timings = [
                    dict(item) for item in raw_timings if isinstance(item, dict)
                ]
                return RecentCallRecord(
                    caller_id=payload["caller_id"],
                    source=payload.get("source", "unknown"),
                    ended_at=float(payload["ended_at"]),
                    transcript=transcript,
                    metadata={
                        str(key): str(value)
                        for key, value in payload.get("metadata", {}).items()
                        if value is not None
                    },
                    subagent_timings=subagent_timings,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load recent call state from %s: %s", path, exc
                )

        return None

    def _parse_state_timestamp(self, value: object) -> float | None:
        if not isinstance(value, str) or not value.strip():
            return None
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def _consume_handoff_bootstrap(self, handoff_id: str | None) -> str | None:
        if not handoff_id:
            return None

        path = self._handoff_bootstrap_path(handoff_id)
        if not path.exists():
            logger.warning("Handoff bootstrap %s not found at %s", handoff_id, path)
            return None

        try:
            payload = json.loads(path.read_text())
        except Exception as exc:
            logger.warning("Failed to load handoff bootstrap %s: %s", path, exc)
            return None

        if payload.get("protocol_version") != 1:
            logger.warning(
                "Ignoring handoff bootstrap %s with unsupported protocol_version=%r",
                handoff_id,
                payload.get("protocol_version"),
            )
            return None
        if payload.get("source") != "voice_handoff":
            logger.warning(
                "Ignoring handoff bootstrap %s with unexpected source=%r",
                handoff_id,
                payload.get("source"),
            )
            return None

        accepted_at = self._parse_state_timestamp(payload.get("accepted_at"))
        if accepted_at is not None:
            age_seconds = time.time() - accepted_at
            if age_seconds > self.resume_window_seconds:
                logger.info(
                    "Ignoring stale handoff bootstrap %s (%ds old)",
                    handoff_id,
                    int(age_seconds),
                )
                return None

        resume_context = str(payload.get("resume_context") or "").strip()
        if not resume_context:
            logger.warning(
                "Ignoring handoff bootstrap %s with empty resume_context", handoff_id
            )
            return None

        try:
            path.unlink()
        except OSError as exc:
            logger.warning(
                "Failed to delete consumed handoff bootstrap %s: %s", path, exc
            )

        logger.info("Consumed handoff bootstrap %s from %s", handoff_id, path)
        return resume_context

    def _build_session_instructions(
        self,
        *,
        caller_id: str | None,
        from_number: str = "",
        handoff_id: str | None = None,
    ) -> str:
        instructions = self._instructions
        if from_number:
            instructions = _build_caller_instructions(
                instructions, from_number, self.workspace
            )

        handoff_resume_context = self._consume_handoff_bootstrap(handoff_id)
        if handoff_resume_context:
            return f"{handoff_resume_context}\n\n{instructions}"

        return _build_resume_instructions(
            instructions,
            self._consume_recent_call(caller_id),
            self.resume_window_seconds,
        )

    def _consume_recent_call(self, caller_id: str | None) -> RecentCallRecord | None:
        if not caller_id:
            return None

        recent_call = self._load_recent_call(caller_id)
        if not recent_call:
            return None

        age_seconds = time.time() - recent_call.ended_at
        if age_seconds > self.resume_window_seconds:
            return None

        pending_task = self._pending_post_calls.pop(caller_id, None)
        if pending_task:
            pending_task.cancel()
            logger.info("Cancelled pending post-call follow-up for %s", caller_id)

        # Delete the resume-state file(s) so a crash-resume can't re-inject the old
        # transcript, but keep archived per-call records for post-call analysis.
        for path in {
            self._recent_call_path(caller_id),
            self._legacy_recent_call_path(caller_id),
        }:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to delete recent call state %s: %s", path, exc)

        logger.info(
            "Resuming recent %s call for %s (%ds old)",
            recent_call.source,
            caller_id,
            int(age_seconds),
        )
        return recent_call

    async def _run_post_call_command(self, caller_id: str, record_path: Path) -> None:
        if not self.post_call_command:
            return

        argv = shlex.split(self.post_call_command)
        if not argv:
            logger.warning("Ignoring empty GPTME_VOICE_POST_CALL_COMMAND")
            return

        env = os.environ.copy()
        env["GPTME_VOICE_POST_CALL_JSON"] = str(record_path)
        env["GPTME_VOICE_CALLER_ID"] = caller_id
        process = await asyncio.create_subprocess_exec(
            *argv,
            str(record_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                logger.info("Cancelling post-call command for %s", caller_id)
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            raise
        if process.returncode != 0:
            logger.error(
                "Post-call command failed for %s (exit=%s): %s",
                caller_id,
                process.returncode,
                (stderr or b"").decode("utf-8", errors="replace").strip(),
            )
            return

        if stdout:
            logger.info(
                "Post-call command output for %s: %s",
                caller_id,
                stdout.decode("utf-8", errors="replace").strip(),
            )

    async def _schedule_post_call(self, caller_id: str, record_path: Path) -> None:
        existing_task = self._pending_post_calls.pop(caller_id, None)
        if existing_task:
            existing_task.cancel()

        if not self.post_call_command:
            return

        async def _runner() -> None:
            task = asyncio.current_task()
            try:
                await asyncio.sleep(self.post_call_delay_seconds)
                await self._run_post_call_command(caller_id, record_path)
            except asyncio.CancelledError:
                raise
            finally:
                # Only remove our own entry — a newer task may have replaced us
                if self._pending_post_calls.get(caller_id) is task:
                    self._pending_post_calls.pop(caller_id)

        self._pending_post_calls[caller_id] = asyncio.create_task(_runner())

    async def _on_call_end(
        self,
        caller_id: str | None,
        source: str,
        transcript: list[TranscriptTurn],
        metadata: dict[str, str],
        tool_bridge: GptmeToolBridge | None = None,
    ) -> None:
        if not caller_id:
            return

        subagent_timings: list[dict[str, object]] = []
        if tool_bridge is not None:
            try:
                subagent_timings = tool_bridge.get_timings()
            except Exception as exc:  # defensive: never block archival on telemetry
                logger.warning("Failed to collect subagent timings: %s", exc)

        record = RecentCallRecord(
            caller_id=caller_id,
            source=source,
            ended_at=time.time(),
            transcript=transcript,
            metadata={k: v for k, v in metadata.items() if v},
            subagent_timings=subagent_timings,
        )
        self._save_recent_call(record)
        record_path = self._save_call_record(record)
        await self._schedule_post_call(caller_id, record_path)

    def _get_local_caller_id(self, websocket) -> str:
        caller_id = websocket.query_params.get("caller_id")
        if caller_id:
            return caller_id
        return "local"

    def _get_local_handoff_id(self, websocket) -> str | None:
        handoff_id = websocket.query_params.get("handoff_id")
        if handoff_id:
            return handoff_id
        return None

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
            if not auth_token:
                logger.warning(
                    "TWILIO_CALLER_ALLOWLIST is set but TWILIO_AUTH_TOKEN is absent — "
                    "the From field is unauthenticated and can be spoofed; "
                    "set TWILIO_AUTH_TOKEN to enforce the allowlist securely."
                )
            if from_number not in allowlist:
                logger.warning(
                    "Rejected call from unlisted number: %s (%d number(s) in allowlist)",
                    from_number,
                    len(allowlist),
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
        stream_sid: str | None = None
        caller_id: str | None = None
        realtime_client: OpenAIRealtimeClient | None = None
        tool_bridge: GptmeToolBridge | None = None
        audio_converter = AudioConverter()
        transcript: list[TranscriptTurn] = []
        metadata: dict[str, str] = {}
        handoff_id: str | None = None

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
                    handoff_id = custom_params.get("handoff_id") or None
                    caller_id = from_number or call_sid or stream_sid
                    instructions = self._build_session_instructions(
                        caller_id=caller_id,
                        from_number=from_number,
                        handoff_id=handoff_id,
                    )
                    metadata = {
                        "from_number": from_number,
                        "call_sid": call_sid,
                        "stream_sid": stream_sid,
                        "provider": self.provider,
                    }
                    if handoff_id:
                        metadata["handoff_id"] = handoff_id

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
                        on_ai_transcript=lambda text: _append_transcript_turn(
                            transcript, "assistant", text
                        ),
                        on_user_transcript=lambda text: _append_transcript_turn(
                            transcript, "user", text
                        ),
                    )
                    hangup_ws = websocket
                    hangup_call_sid = call_sid

                    async def _twilio_hangup(reason: str | None) -> None:
                        await self._schedule_hangup(
                            hangup_ws,
                            source="twilio",
                            reason=reason,
                            call_sid=hangup_call_sid,
                        )

                    tool_bridge = GptmeToolBridge(
                        workspace=self.workspace,
                        on_result=realtime_client.inject_message,
                        on_hangup=_twilio_hangup,
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
                    break

        except WebSocketDisconnect:
            pass  # Normal path when _schedule_hangup closes the WebSocket
        except RuntimeError as exc:
            # Starlette raises RuntimeError from receive() when the socket is
            # already closed (e.g. after _schedule_hangup closes server-side).
            # Treat that as a normal disconnect instead of logging a traceback.
            if "not connected" not in str(exc).lower():
                raise
            logger.debug("Twilio websocket already closed before iter_text: %s", exc)
        except Exception as e:
            logger.exception("Error handling Twilio connection: %s", e)
        finally:
            if realtime_client:
                await self._disconnect_realtime_client(realtime_client)
            if call_sid and call_sid in self._connections:
                del self._connections[call_sid]
            await self._on_call_end(
                caller_id,
                "twilio",
                transcript,
                metadata,
                tool_bridge=tool_bridge,
            )

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

        caller_id = self._get_local_caller_id(websocket)
        handoff_id = self._get_local_handoff_id(websocket)
        realtime_client: OpenAIRealtimeClient | None = None
        tool_bridge: GptmeToolBridge | None = None
        transcript: list[TranscriptTurn] = []

        try:
            instructions = self._build_session_instructions(
                caller_id=caller_id,
                handoff_id=handoff_id,
            )
            if self.model:
                session_cfg = SessionConfig(instructions=instructions, model=self.model)
            else:
                session_cfg = SessionConfig(instructions=instructions)
            realtime_client = self._make_client(
                session_cfg,
                on_audio=lambda audio: self._send_local_audio(websocket, audio),
                on_audio_end=lambda: self._send_local_audio_end(websocket),
                on_ai_transcript=lambda text: _append_transcript_turn(
                    transcript, "assistant", text
                ),
                on_user_transcript=lambda text: _append_transcript_turn(
                    transcript, "user", text
                ),
            )
            local_ws = websocket

            async def _local_hangup(reason: str | None) -> None:
                await self._schedule_hangup(
                    local_ws,
                    source="local",
                    reason=reason,
                    call_sid=None,
                )

            tool_bridge = GptmeToolBridge(
                workspace=self.workspace,
                on_result=realtime_client.inject_message,
                on_hangup=_local_hangup,
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

        except WebSocketDisconnect:
            pass  # Normal path when _schedule_hangup closes the WebSocket
        except RuntimeError as exc:
            # Starlette raises RuntimeError from receive() when the socket is
            # already closed (e.g. after _schedule_hangup closes server-side).
            # Treat that as a normal disconnect instead of logging a traceback.
            if "not connected" not in str(exc).lower():
                raise
            logger.debug("Local websocket already closed before iter_text: %s", exc)
        except Exception as e:
            logger.exception("Error handling local connection: %s", e)
        finally:
            if realtime_client:
                await self._disconnect_realtime_client(realtime_client)
            await self._on_call_end(
                caller_id,
                "local",
                transcript,
                {
                    "caller_id": caller_id,
                    "provider": self.provider,
                    **({"handoff_id": handoff_id} if handoff_id else {}),
                },
                tool_bridge=tool_bridge,
            )

    async def _schedule_hangup(
        self,
        websocket,
        *,
        source: str,
        reason: str | None,
        call_sid: str | None,
    ) -> None:
        """Close the call-side WebSocket after a short delay.

        Runs from a background task spawned by the tool bridge. The delay lets
        the model finish its farewell utterance before the socket drops. When
        the socket closes, the ``handle_*_websocket`` loop exits its
        ``async for`` and falls through to the ``finally`` block, which runs
        the normal ``_on_call_end`` teardown (post-call hook, transcript
        persistence, resume record).
        """
        logger.info(
            "Hangup scheduled: source=%s call_sid=%s reason=%s",
            source,
            call_sid,
            reason or "<none>",
        )
        try:
            await asyncio.sleep(_HANGUP_FAREWELL_DELAY_SECONDS)
        except asyncio.CancelledError:
            raise
        try:
            await websocket.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Error closing WebSocket during hangup: %s", exc)

    async def _disconnect_realtime_client(
        self, realtime_client: OpenAIRealtimeClient
    ) -> None:
        """Drain late transcript events briefly before closing the provider socket."""

        await realtime_client.disconnect(
            drain_timeout_seconds=_CALL_END_DRAIN_TIMEOUT_SECONDS,
            idle_timeout_seconds=_CALL_END_IDLE_TIMEOUT_SECONDS,
            commit_audio=True,
            stop_audio_output=True,
        )

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
