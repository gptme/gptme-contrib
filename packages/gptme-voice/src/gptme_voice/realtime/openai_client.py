"""
OpenAI Realtime API WebSocket client.

Handles bidirectional audio streaming and function calling for real-time
voice conversations.
"""

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Callable, Optional, Any
from dataclasses import dataclass

import websockets  # type: ignore
from gptme.config import get_config, get_project_config

logger = logging.getLogger(__name__)

_DEFAULT_INSTRUCTIONS = "You are a helpful assistant with access to tools via gptme."


def _get_openai_api_key() -> Optional[str]:
    """Get OpenAI API key from gptme config (env var, project, or user config)."""
    config = get_config()
    return config.get_env("OPENAI_API_KEY")


# Files to prioritize for voice personality (in order of preference)
_PERSONALITY_FILES = ["ABOUT.md", "README.md"]

# Max chars for instructions (realtime API has limits)
_MAX_INSTRUCTIONS_LEN = 4096


def _detect_agent_repo() -> Optional[str]:
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


def _load_project_instructions(workspace: Optional[str] = None) -> str:
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

    if not parts:
        return _DEFAULT_INSTRUCTIONS

    preamble = (
        "You are in a real-time voice conversation. "
        "Keep responses concise and conversational. "
        "IMPORTANT: When asked about your recent activity, tasks, journal entries, "
        "code changes, or anything factual about your workspace — ALWAYS use the "
        "subagent tool to look it up. Never guess or hallucinate facts about what "
        "you've been doing. Only speak from the personality context below for "
        "identity questions (who you are, your values, etc).\n\n"
        "Below is your personality and context:\n\n"
    )
    result = preamble + "\n\n---\n\n".join(parts)

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
        on_audio_end: Optional[Callable[[], None]] = None,
        on_transcript: Optional[Callable[[str], None]] = None,
        on_function_call: Optional[Callable[[str, dict], Any]] = None,
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
        self.on_function_call = on_function_call

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._responding = False  # True while AI is generating a response

    async def connect(self) -> None:
        """Connect to OpenAI Realtime API."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        url = f"{self.WS_URL}?model={self.session_config.model}"
        self._ws = await websockets.connect(url, additional_headers=headers)

        instructions = self.session_config.instructions or _DEFAULT_INSTRUCTIONS
        logger.info(
            f"Session instructions ({len(instructions)} chars): {instructions[:100]}..."
        )

        # Configure session
        await self._send_event(
            "session.update",
            {
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": instructions,
                    "voice": self.session_config.voice,
                    "input_audio_format": self.session_config.input_format,
                    "output_audio_format": self.session_config.output_format,
                    "input_audio_transcription": {"model": "whisper-1"},
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
                                "The subagent has full access to tools: shell, file read/write, "
                                "python, and can reason about multi-step tasks. "
                                "Use this for anything that requires interacting with the codebase, "
                                "reading files, checking task status, running commands, searching code, etc. "
                                "Describe what you want done in natural language."
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
                                            "Model quality tradeoff. 'smart' (default) uses the full model "
                                            "for complex tasks like code analysis, multi-step reasoning, or "
                                            "writing code. 'fast' uses a smaller model for quick lookups like "
                                            "reading a file, checking git status, or simple searches."
                                        ),
                                    },
                                },
                                "required": ["task"],
                            },
                        }
                    ],
                }
            },
        )

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

        Audio is always forwarded so the server-side VAD can detect
        speech interruptions. Feedback loop prevention (speaker output
        being picked up by mic) is handled client-side.
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

        # User speech transcript
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                logger.info(f"User: {transcript}")

        # VAD events
        elif event_type == "input_audio_buffer.speech_started":
            logger.debug("Speech detected")
        elif event_type == "input_audio_buffer.speech_stopped":
            logger.debug("Speech ended")

        # Session events
        elif event_type == "session.created":
            logger.info("Session created")
        elif event_type == "session.updated":
            logger.info("Session configured")

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
