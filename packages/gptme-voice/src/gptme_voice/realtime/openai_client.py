"""
OpenAI Realtime API WebSocket client.

Handles bidirectional audio streaming and function calling for real-time
voice conversations.
"""

import asyncio
import base64
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import websockets  # type: ignore
from gptme.config import get_config, get_project_config

logger = logging.getLogger(__name__)

_DEFAULT_INSTRUCTIONS = "You are a helpful assistant with access to tools via gptme."


def _get_openai_api_key() -> str | None:
    """Get OpenAI API key from gptme config (env var, project, or user config)."""
    config = get_config()
    return config.get_env("OPENAI_API_KEY")


# Files to prioritize for voice personality (in order of preference)
_PERSONALITY_FILES = ["ABOUT.md", "README.md"]

# Max chars for instructions (realtime API has limits)
_MAX_INSTRUCTIONS_LEN = 4096

# Max audio chunks to buffer before session.created arrives. Twilio sends
# media frames every 20ms (50/sec), so 500 chunks ≈ 10s of audio — more
# than enough for any realistic session-handshake delay while still capping
# memory growth if the provider never confirms the session.
_MAX_PENDING_AUDIO_CHUNKS = 500
# Event types that carry transcript text — only these reset the drain idle timer.
_TRANSCRIPT_EVENT_TYPES = frozenset(
    {
        "response.audio_transcript.delta",
        "response.output_audio_transcript.delta",
        "response.audio_transcript.done",
        "response.output_audio_transcript.done",
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
    }
)


def _detect_agent_repo() -> str | None:
    """Auto-detect the agent repo root.

    Assumes gptme-contrib is a subdirectory of the agent repo
    (as in agent repos that include gptme-contrib as a subdirectory).
    Walks up from this package to find a directory containing gptme-contrib/.
    """
    # This file is at: agent-repo/gptme-contrib/packages/gptme-voice/src/gptme_voice/realtime/
    p = Path(__file__).resolve()
    for parent in p.parents:
        if parent.name == "gptme-contrib" and (parent.parent / "gptme.toml").exists():
            repo = parent.parent
            logger.info(f"Auto-detected agent repo: {repo}")
            return str(repo)
    return None


def _load_project_instructions(workspace: str | None = None) -> str:
    """Load personality/instructions from gptme project config files.

    Loads personality-relevant files from the workspace's gptme.toml config,
    prioritizing ABOUT.md and keeping instructions concise for voice mode.
    """
    if not workspace:
        return _DEFAULT_INSTRUCTIONS

    ws_path = Path(workspace)
    project_config = get_project_config(ws_path)
    if not project_config or not project_config.files:
        return _DEFAULT_INSTRUCTIONS

    # Load personality files first, then fill with others up to limit
    parts = []
    loaded = set()

    # Priority files first
    for name in _PERSONALITY_FILES:
        if name in project_config.files:
            p = ws_path / name
            if p.is_file():
                try:
                    content = p.read_text()
                    parts.append(f"# {p.name}\n{content}")
                    loaded.add(name)
                except Exception as e:
                    logger.warning(f"Failed to read {p}: {e}")

    # Then other config files, respecting length limit
    for file_pattern in project_config.files:
        if file_pattern in loaded:
            continue
        for p in sorted(ws_path.glob(file_pattern)):
            if p.is_file():
                try:
                    content = p.read_text()
                    parts.append(f"# {p.name}\n{content}")
                    loaded.add(file_pattern)
                except Exception as e:
                    logger.warning(f"Failed to read {p}: {e}")
        # Check length
        total = sum(len(p) for p in parts)
        if total > _MAX_INSTRUCTIONS_LEN:
            break

    # Build guards preamble — always applied regardless of personality files so
    # behavioral constraints are never silently absent for a live-call session.
    preamble = (
        "You are in a real-time voice conversation. "
        "Keep responses concise and conversational.\n\n"
        "SUBAGENT TOOL RULES:\n"
        "- Use the subagent tool ONLY for small, specific lookups: a single task status, "
        "a recent journal entry, a quick file check. One focused question per call.\n"
        "- Do NOT dispatch broad investigation tasks (e.g. 'investigate the whole system', "
        "'run a full review') — these always time out and leave the call hanging.\n"
        "- NEVER use the subagent tool to run post-call analysis, summarise the session, "
        "or queue follow-up work. That is handled automatically by the server after the "
        "call ends. Just say goodbye naturally — the post-call job fires on its own.\n"
        "- When asked about recent activity, tasks, journal entries, or workspace facts, "
        "use the subagent tool to look up the specific thing asked. Never guess.\n\n"
        "SUBAGENT STATUS AND CANCEL:\n"
        "- If the caller asks what the subagent is doing, call the subagent_status tool "
        "to list pending tasks — do not use a fresh subagent dispatch for this.\n"
        "- If the caller asks to cancel the subagent, call the subagent_cancel tool. "
        "Pass task_id for a specific task, or omit task_id to cancel all pending tasks.\n"
        "- Do not promise to 'try to stop it' verbally without calling subagent_cancel.\n\n"
        "POST-CALL FOLLOW-UP:\n"
        "- Post-call analysis and follow-up run automatically after the call ends. "
        "They are triggered by the server on hangup, not by you.\n"
        "- Do NOT claim, announce, or imply that you have dispatched, started, or queued "
        "post-call work during the live call, even verbally without a tool call. "
        "Saying 'post-call analysis dispatched' inside a call is wrong — it has not "
        "happened yet and you are not the one who starts it.\n"
        "- It is fine to acknowledge that follow-up will happen automatically after "
        "hangup if the user asks. Just do not take credit for dispatching it.\n\n"
        "HANDOFF TO ANOTHER AGENT:\n"
        "- Use handoff_to_agent ONLY when the caller explicitly asks to speak with "
        "Alice, Gordon, or Sven, or when the topic is clearly outside your expertise "
        "and another specific agent is better suited.\n"
        "- Always say a brief handoff notice before calling the tool "
        "(e.g. 'I'll transfer you to Alice now — one moment.').\n"
        "- The full transcript is forwarded automatically. You don't need to summarise "
        "the conversation unless there's important context not obvious from the transcript.\n"
        "- Do not use handoff as a way to avoid answering a question.\n\n"
    )

    if not parts:
        return preamble  # guards still apply even with no personality files

    result = (
        preamble
        + "Below is your personality and context:\n\n"
        + "\n\n---\n\n".join(parts)
    )

    # Truncate if still too long
    if len(result) > _MAX_INSTRUCTIONS_LEN:
        result = (
            result[: _MAX_INSTRUCTIONS_LEN - 100] + "\n\n[truncated for voice mode]"
        )

    logger.info(f"Loaded {len(loaded)} personality files ({len(result)} chars)")
    return result


@dataclass
class SessionConfig:
    """Configuration for OpenAI Realtime API session."""

    model: str = "gpt-4o-realtime-preview-2024-12-17"
    voice: str = "echo"
    instructions: str = ""
    input_format: str = "pcm16"
    output_format: str = "pcm16"
    input_sample_rate: int = 24000
    output_sample_rate: int = 24000
    turn_detection: str = "server_vad"
    vad_threshold: float = 0.7
    vad_silence_duration_ms: int = 500
    vad_prefix_padding_ms: int = 300
    available_agents: list[str] = field(
        default_factory=lambda: ["alice", "gordon", "sven"]
    )


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
        api_key: str | None = None,
        session_config: SessionConfig | None = None,
        on_audio: Callable[[bytes], None] | None = None,
        on_audio_end: Callable[[], None] | None = None,
        on_transcript: Callable[[str], None] | None = None,
        on_ai_transcript: Callable[[str], None] | None = None,
        on_user_transcript: Callable[[str], None] | None = None,
        on_function_call: Callable[[str, dict], Any] | None = None,
    ):
        self.api_key = api_key or _get_openai_api_key()
        if not self.api_key:
            raise ValueError(
                "OPENAI_API_KEY not found. Set it in gptme config or as an env var."
            )

        self.session_config = session_config or SessionConfig()
        self.on_audio = on_audio
        self.on_audio_end = on_audio_end
        self.on_transcript = on_transcript
        self.on_ai_transcript = on_ai_transcript
        self.on_user_transcript = on_user_transcript
        self.on_function_call = on_function_call

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._receive_task: asyncio.Task | None = None
        self._responding = False  # True while AI is generating a response

        # Audio arriving before `session.created` would be forwarded to a session
        # that does not exist yet, causing silent calls (observed on cold starts
        # and cold reconnects). Buffer early audio and flush on session ready.
        # Cap buffer size so a never-arriving session.created cannot leak memory.
        self._session_ready: asyncio.Event | None = None
        self._pending_audio: list[bytes] = []
        self._pending_audio_dropped = 0
        self._event_notice: asyncio.Event | None = None

    def _get_ws_url(self) -> str:
        """WebSocket URL for this provider (override in subclasses)."""
        return f"{self.WS_URL}?model={self.session_config.model}"

    def _get_ws_headers(self) -> dict[str, str]:
        """Auth headers for this provider (override in subclasses)."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

    def _get_transcription_config(self) -> dict | None:
        """Transcription config for session.update (override to None to omit)."""
        return {"model": "whisper-1"}

    async def connect(self) -> None:
        """Connect to OpenAI Realtime API."""
        # Initialize session-ready gate inside the event loop that will drive
        # this connection. Allows the client to be re-connected after disconnect.
        self._session_ready = asyncio.Event()
        self._pending_audio = []
        self._pending_audio_dropped = 0
        self._event_notice = asyncio.Event()

        url = self._get_ws_url()
        headers = self._get_ws_headers()
        self._ws = await websockets.connect(url, additional_headers=headers)

        instructions = self.session_config.instructions or _DEFAULT_INSTRUCTIONS
        logger.info(
            f"Session instructions ({len(instructions)} chars): {instructions[:100]}..."
        )

        # Configure session
        session_params: dict = {
            "modalities": ["text", "audio"],
            "instructions": instructions,
            "voice": self.session_config.voice,
            "input_audio_format": self.session_config.input_format,
            "output_audio_format": self.session_config.output_format,
            "turn_detection": {
                "type": self.session_config.turn_detection,
                "threshold": self.session_config.vad_threshold,
                "silence_duration_ms": self.session_config.vad_silence_duration_ms,
                "prefix_padding_ms": self.session_config.vad_prefix_padding_ms,
            },
            "tools": [
                {
                    "type": "function",
                    "name": "subagent",
                    "description": (
                        "Dispatch a task to a gptme subagent running in the workspace. "
                        "Use it only for one small, focused workspace lookup or action: "
                        "check one task, inspect one file, run one quick command, or verify "
                        "one recent fact. Do not use it for broad investigations, full "
                        "reviews, or post-call analysis. Describe one concrete request in "
                        "natural language."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Natural language description of the task for the subagent",
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["smart", "fast"],
                                "description": (
                                    "Response urgency. 'fast' uses a smaller model for speed — "
                                    "prefer this for simple lookups. 'smart' (default) uses a "
                                    "larger model when accuracy matters. Both are for small, "
                                    "focused lookups only — never for broad investigations."
                                ),
                            },
                        },
                        "required": ["task"],
                    },
                },
                {
                    "type": "function",
                    "name": "subagent_status",
                    "description": (
                        "Check which subagent tasks are still running. Use this when "
                        "the caller asks what the subagent is doing, or before deciding "
                        "to cancel. Returns each pending task's id, a short preview of "
                        "the task, the mode, and elapsed seconds."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
                {
                    "type": "function",
                    "name": "subagent_cancel",
                    "description": (
                        "Cancel a running subagent task. Use this when the caller "
                        "explicitly asks to stop or cancel the subagent, or when the "
                        "dispatched task no longer matches what the caller wants. "
                        "Pass task_id to cancel a specific task, or omit task_id to "
                        "cancel every pending subagent task."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": (
                                    "Task id to cancel (as returned by subagent or "
                                    "subagent_status). Omit task_id to cancel every "
                                    "pending subagent task."
                                ),
                            },
                        },
                    },
                },
                {
                    "type": "function",
                    "name": "hangup",
                    "description": (
                        "End the current voice call cleanly. Use this only when the caller "
                        "has clearly said goodbye or explicitly asked to hang up. Do not use "
                        "this to interrupt ongoing work or avoid a question. "
                        "Say a brief farewell first; the call will terminate shortly after."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": (
                                    "Short free-form reason for hanging up "
                                    "(e.g. 'caller said goodbye'). Optional."
                                ),
                            },
                        },
                    },
                },
                {
                    "type": "function",
                    "name": "handoff_to_agent",
                    "description": (
                        "Transfer the caller to another AI agent ("
                        + ", ".join(
                            a.capitalize() for a in self.session_config.available_agents
                        )
                        + "). "
                        "Use this when the caller explicitly asks to speak with a different "
                        "agent, or when the topic is clearly outside your expertise and "
                        "another agent is better suited. Say a brief handoff notice first "
                        "(e.g. 'I'll transfer you to Alice now'). The transfer includes the "
                        "full conversation transcript so the receiving agent has context."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to_agent": {
                                "type": "string",
                                "enum": self.session_config.available_agents,
                                "description": "The agent to transfer the caller to.",
                            },
                            "reason": {
                                "type": "string",
                                "description": (
                                    "Short reason for the transfer "
                                    "(e.g. 'caller asked to speak with Alice'). Required."
                                ),
                            },
                            "context_summary": {
                                "type": "string",
                                "description": (
                                    "Optional brief summary of the conversation for the "
                                    "receiving agent (max 500 chars). Use this to highlight "
                                    "key context not obvious from the transcript."
                                ),
                            },
                        },
                        "required": ["to_agent", "reason"],
                    },
                },
            ],
        }
        transcription = self._get_transcription_config()
        if transcription is not None:
            session_params["input_audio_transcription"] = transcription
        await self._send_event("session.update", {"session": session_params})

        # Start receiving messages
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def inject_message(self, text: str) -> None:
        """Inject a message into the conversation and trigger a response.

        Used to deliver async subagent results back into the voice conversation.
        """
        logger.info(f"Injecting message: {text[:100]}...")
        await self._send_event(
            "conversation.item.create",
            {
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[Subagent result]: {text}",
                        }
                    ],
                }
            },
        )
        await self._send_event("response.create", {})

    async def disconnect(
        self,
        *,
        drain_timeout_seconds: float = 0.0,
        idle_timeout_seconds: float = 0.0,
        commit_audio: bool = False,
        stop_audio_output: bool = False,
    ) -> None:
        """Disconnect from OpenAI Realtime API."""
        if stop_audio_output:
            # Once the call-side websocket is closing, late provider audio is
            # more trouble than it's worth. Keep transcript callbacks alive so
            # we can still persist the final text turns during the drain window.
            self.on_audio = None
            self.on_audio_end = None

        if (
            commit_audio
            and self._session_ready is not None
            and self._session_ready.is_set()
        ):
            with contextlib.suppress(Exception):
                await self.commit_audio()

        if drain_timeout_seconds > 0 and idle_timeout_seconds > 0:
            # CancelledError must not skip the cleanup below (receive_task cancel
            # + ws.close). Suppress it here; caller's finally block still runs.
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_incoming_events(
                    timeout_seconds=drain_timeout_seconds,
                    idle_timeout_seconds=idle_timeout_seconds,
                )

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _drain_incoming_events(
        self, *, timeout_seconds: float, idle_timeout_seconds: float
    ) -> None:
        """Wait briefly for late provider events before disconnecting.

        This is primarily for call teardown: a caller can hang up while the
        realtime provider is still about to emit the final transcript turn.
        Keeping the provider socket alive for a short idle-bounded window lets
        those late text events land without keeping the connection open forever.
        """

        if timeout_seconds <= 0 or idle_timeout_seconds <= 0:
            return

        receive_task = self._receive_task
        event_notice = self._event_notice
        if receive_task is None or receive_task.done() or event_notice is None:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while not receive_task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return

            event_notice.clear()
            try:
                await asyncio.wait_for(
                    event_notice.wait(),
                    timeout=min(idle_timeout_seconds, remaining),
                )
            except asyncio.TimeoutError:
                return

    async def send_audio(self, pcm_data: bytes) -> None:
        """
        Send audio to OpenAI Realtime API.

        Audio is always forwarded so the server-side VAD can detect
        speech interruptions. Feedback loop prevention (speaker output
        being picked up by mic) is handled client-side.

        Audio that arrives before the provider has confirmed the session
        is buffered and flushed once the session is ready. This prevents
        silent calls on cold starts / fast reconnects where Twilio's first
        media frames race ahead of the provider's session handshake.
        """
        if self._session_ready is None or not self._session_ready.is_set():
            # Session not confirmed yet — buffer the chunk, bounded so a
            # never-arriving ready signal cannot leak memory.
            if len(self._pending_audio) < _MAX_PENDING_AUDIO_CHUNKS:
                self._pending_audio.append(pcm_data)
            else:
                self._pending_audio_dropped += 1
                if self._pending_audio_dropped == 1:
                    logger.warning(
                        "Dropping pre-session audio (buffer full at %d chunks) — "
                        "provider ready signal may be delayed",
                        _MAX_PENDING_AUDIO_CHUNKS,
                    )
            return

        audio_b64 = base64.b64encode(pcm_data).decode("utf-8")
        await self._send_event("input_audio_buffer.append", {"audio": audio_b64})

    async def _mark_session_ready(self, event_type: str) -> None:
        """Mark the provider session ready and flush any buffered audio once."""
        if self._session_ready is None or self._session_ready.is_set():
            return

        logger.info("%s received — marking session ready", event_type)
        self._session_ready.set()
        await self._flush_pending_audio()

    async def _flush_pending_audio(self) -> None:
        """Send any audio that was buffered before the session was ready."""
        if not self._pending_audio:
            return
        chunks = self._pending_audio
        self._pending_audio = []
        logger.info(
            "Flushing %d buffered audio chunk(s) after session ready",
            len(chunks),
        )
        for pcm_data in chunks:
            audio_b64 = base64.b64encode(pcm_data).decode("utf-8")
            await self._send_event("input_audio_buffer.append", {"audio": audio_b64})
        if self._pending_audio_dropped:
            logger.warning(
                "Dropped %d pre-session audio chunk(s) before flush",
                self._pending_audio_dropped,
            )
            self._pending_audio_dropped = 0

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
        # Only transcript-carrying events reset the drain idle timer; VAD and
        # lifecycle events must not extend the teardown window unnecessarily.
        if self._event_notice is not None and event_type in _TRANSCRIPT_EVENT_TYPES:
            self._event_notice.set()

        # Audio output chunk (handle both old and new event names)
        if event_type in ("response.audio.delta", "response.output_audio.delta"):
            if self.on_audio:
                audio_b64 = event.get("delta", "")
                audio_data = base64.b64decode(audio_b64)
                await self._call_callback(self.on_audio, audio_data)

        # Audio output finished
        elif event_type in ("response.audio.done", "response.output_audio.done"):
            logger.debug("Audio response complete")
            if self.on_audio_end:
                await self._call_callback(self.on_audio_end)

        # Transcript of AI response
        elif event_type in (
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
        ):
            if self.on_transcript:
                transcript = event.get("delta", "")
                await self._call_callback(self.on_transcript, transcript)

        # Transcript done - log full transcript
        elif event_type in (
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
        ):
            transcript = event.get("transcript", "")
            if transcript:
                logger.info(f"AI: {transcript}")
                if self.on_ai_transcript:
                    await self._call_callback(self.on_ai_transcript, transcript)

        # User speech transcript
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                logger.info(f"User: {transcript}")
                if self.on_user_transcript:
                    await self._call_callback(self.on_user_transcript, transcript)

        # VAD events
        elif event_type == "input_audio_buffer.speech_started":
            logger.debug("Speech detected")
        elif event_type == "input_audio_buffer.speech_stopped":
            logger.debug("Speech ended")

        # Session events
        elif event_type == "session.created":
            logger.info("Session created")
            await self._mark_session_ready(event_type)
        elif event_type == "session.updated":
            logger.info("Session configured")
            await self._mark_session_ready(event_type)

        # Response lifecycle — mute mic while responding
        elif event_type == "response.created":
            self._responding = True
            logger.debug("Response started (mic muted)")
        elif event_type == "response.done":
            self._responding = False
            status = event.get("response", {}).get("status", "")
            logger.debug(f"Response done: {status} (mic unmuted)")

        # Function call (handle both old and new event names)
        elif event_type in (
            "response.function_call",
            "response.function_call_arguments.done",
        ):
            if self.on_function_call:
                call_id = event.get("call_id", "")
                name = event.get("name", "")
                arguments = json.loads(event.get("arguments", "{}"))
                logger.info(f"Function call: {name}({arguments})")
                result = await self._call_callback(
                    self.on_function_call, name, arguments
                )
                # Send function result back
                await self._send_event(
                    "conversation.item.create",
                    {
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result) if result else "",
                        }
                    },
                )
                # Trigger a new response after function output
                await self._send_event("response.create", {})

        # Errors
        elif event_type == "error":
            logger.error(f"API error: {event.get('error', {})}")

    async def _call_callback(self, callback: Callable, *args) -> Any:
        """Call a callback, handling both sync and async functions."""
        import inspect

        result = callback(*args)
        if inspect.iscoroutine(result):
            result = await result
        return result
