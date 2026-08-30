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
import re
import secrets
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import click
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from ..body import body_adapter_from_env, body_tool_schemas
from ..handoff import HandoffWriter
from ..vision import VisionSessionBridge, vision_tool_schema
from .audio import AudioConverter
from .openai_client import (
    OpenAIRealtimeClient,
    SessionConfig,
    _detect_agent_repo,
    _get_openai_api_key,
    _load_project_instructions,
)
from .sounds import DISPATCH_CUE_MULAW, PCM_CUES, SAMPLE_RATE, TIMEOUT_CUE_MULAW
from .tool_bridge import GptmeToolBridge
from .twilio_integration import (
    _get_config_env,
    build_connect_stream_twiml,
    build_stream_url,
)
from .xai_client import XAIRealtimeClient, _get_xai_api_key


def _websocket_peer_host(websocket) -> str | None:
    """Peer host from a Starlette (or mock) websocket.

    Starlette's ``websocket.client`` is ``Address(host, port)`` — a namedtuple
    with a ``.host`` field that is also a plain ``(host, port)`` tuple. ASGI
    scope values and some test doubles are just the tuple. Accept both; never
    assume ``.host`` exists.
    """
    client = getattr(websocket, "client", None)
    if client is None:
        return None
    host = getattr(client, "host", None)
    if isinstance(host, str) and host:
        return host
    if isinstance(client, tuple | list) and client:
        first = client[0]
        if isinstance(first, str) and first:
            return first
    return None


logger = logging.getLogger(__name__)


@dataclass
class CallerIdentity:
    canonical_name: str
    preferred_spoken_name: str
    # True when the caller's people file marks them as the operator
    # (`- Call role: operator`).  Operators get internal context (activity
    # digest, ops status); everyone else is treated as an external guest.
    is_operator: bool = False
    # Preferred spoken language from `- Call language: <lang>`, if present.
    call_language: str | None = None


_DEFAULT_RESUME_WINDOW_SECONDS = 300
_DEFAULT_STATE_DIR = "/tmp/gptme-voice-call-state"
_MAX_RESUME_TRANSCRIPT_CHARS = 2500


def _http_post_sync(url: str, payload: bytes, api_key: str) -> None:
    """Fire-and-forget HTTP POST to the gptme server transcript endpoint.

    Runs in a thread-pool executor so it never blocks the event loop.
    Failures are logged but never raised — transcript promotion is
    best-effort.
    """
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if 200 <= resp.status < 300:
                logger.info(
                    "Promoted transcript to gptme server (%d bytes)", len(payload)
                )
            else:
                logger.warning(
                    "Unexpected %d from transcript endpoint: %s",
                    resp.status,
                    body[:200],
                )
    except urllib.error.HTTPError as exc:
        logger.warning(
            "Transcript promotion HTTP %d: %s",
            exc.code,
            exc.read().decode("utf-8", errors="replace")[:200],
        )
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Transcript promotion failed (network): %s", exc)


# Brief pause so any queued farewell audio still reaches the caller.
# The old 5.0s delay was a problem because it kept the call accepting audio
# long after the model said goodbye. The real termination now happens via the
# Twilio REST API (calls.update(status='completed')), so the WebSocket close
# delay only needs to be long enough for buffered audio to drain.
_HANGUP_FAREWELL_DELAY_SECONDS = 0.5
_CALL_END_DRAIN_TIMEOUT_SECONDS = 1.5
_CALL_END_IDLE_TIMEOUT_SECONDS = 0.25
_USER_HANGUP_INTENT_RE = re.compile(
    r"\b(?:bye|goodbye|hang\s*up|end(?:ing)?\s+(?:the\s+)?call|disconnect)\b",
    re.IGNORECASE,
)
_ASSISTANT_HANGUP_COMMIT_RE = re.compile(
    r"(?:"
    r"\bi(?:'ll| will)\s+(?:hang\s*up|end(?:\s+the)?\s+call)"
    r"(?:\s+(?:now|shortly|right now))?\b"
    r"|"
    r"\bi(?:'ll| will)\s+call\s+the\s+hangup\s+tool"
    r"(?:\s+to\s+end\s+the\s+call)?(?:\s+(?:now|right now))?\b"
    r"|"
    r"\bcalling\s+hangup\s+tool\s+now\b"
    r"|"
    r"\bending\s+(?:the\s+)?call\s+now\b"
    r")",
    re.IGNORECASE,
)
_ASSISTANT_HANGUP_DISQUALIFIERS_RE = re.compile(
    r"\b(?:"
    r"would you like|"
    r"if you'd like|"
    r"if you would like|"
    r"if you want|"
    r"can hang up|"
    r"could hang up|"
    r"should i hang up|"
    r"shall i hang up"
    r")\b|\?",
    re.IGNORECASE,
)


@dataclass
class TranscriptTurn:
    role: str
    text: str
    item_id: str | None = None


@dataclass
class RecentCallRecord:
    caller_id: str
    source: str
    ended_at: float
    transcript: list[TranscriptTurn]
    metadata: dict[str, str]
    subagent_timings: list[dict[str, object]] = field(default_factory=list)
    archive_record_paths: list[str] = field(default_factory=list)
    pending_post_call_unit: str | None = None


@dataclass
class SessionBootstrap:
    instructions: str
    should_greet_first: bool = False
    initial_response_instructions: str = ""


def _extract_people_field(text: str, field: str) -> str | None:
    """Extract a `- <field>: value` line from a people file (case-insensitive).

    Handles markdown-bolded property names/values too, e.g.
    `- **Call name**: Erik` or `- Call name: **Erik**`.
    """
    pattern = re.compile(
        rf"^-\s*\*{{0,2}}{re.escape(field)}\*{{0,2}}\s*:\s*(.+)$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        m = pattern.match(line.strip())
        if m:
            value = m.group(1).strip().strip("*").strip()
            if value:
                return value
    return None


def _extract_preferred_spoken_name(text: str, canonical_name: str) -> str:
    value = _extract_people_field(text, "call name")
    if value:
        return value
    parts = canonical_name.split()
    return parts[0] if parts else canonical_name


def _lookup_caller_identity(
    from_number: str, workspace: str | None
) -> CallerIdentity | None:
    if workspace:
        people_dir = Path(workspace) / "people"
        if people_dir.is_dir():
            for md_file in people_dir.glob("*.md"):
                try:
                    text = md_file.read_text()
                    if from_number in text:
                        first_h1 = next(
                            (
                                line.lstrip("# ").strip()
                                for line in text.splitlines()
                                if line.startswith("# ")
                            ),
                            None,
                        )
                        canonical_name = (
                            first_h1 or md_file.stem.replace("-", " ").title()
                        )
                        preferred_spoken_name = _extract_preferred_spoken_name(
                            text, canonical_name
                        )
                        call_role = _extract_people_field(text, "call role")
                        return CallerIdentity(
                            canonical_name=canonical_name,
                            preferred_spoken_name=preferred_spoken_name,
                            is_operator=(call_role or "").lower() == "operator",
                            call_language=_extract_people_field(text, "call language"),
                        )
                except Exception:
                    pass
    return None


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

    caller_identity = _lookup_caller_identity(from_number, workspace)

    if caller_identity:
        name_hint = (
            f" On voice calls, prefer '{caller_identity.preferred_spoken_name}' over their full name."
            if caller_identity.preferred_spoken_name != caller_identity.canonical_name
            else ""
        )
        caller_ctx = (
            f"The current caller's phone number is {from_number} "
            f"({caller_identity.canonical_name}). "
            f"You know this person — refer to them by name."
            f"{name_hint}"
        )
        if not caller_identity.is_operator:
            caller_ctx += "\n\n" + _external_caller_guidance(caller_identity)
    else:
        caller_ctx = (
            f"The current caller's phone number is {from_number}. "
            f"You do not recognise this number; treat the caller as an unknown guest. "
            + _external_caller_guidance(None)
        )

    return f"{caller_ctx}\n\n{base_instructions}"


def _external_caller_guidance(caller_identity: CallerIdentity | None) -> str:
    """Conversation guidance for callers who are not the operator.

    External callers should get a friendly host, not an ops report: no
    unprompted tool calls, no internal status (subagent state, task queues,
    work summaries), and speech in their language.
    """
    if caller_identity and caller_identity.call_language:
        language_line = (
            f"Speak {caller_identity.call_language} with this caller "
            "unless they switch language themselves."
        )
    else:
        language_line = (
            "Mirror the caller's language: if they speak another language "
            "than English, answer in that language."
        )
    return (
        "EXTERNAL CALLER GUIDANCE:\n"
        "- This caller is NOT the operator. Do not volunteer internal "
        "operational status (subagent status, task queues, PR/CI state, or "
        "work summaries), and do not call tools unprompted.\n"
        "- Be a friendly, concise host: answer their questions, and only use "
        "tools when their request genuinely needs one.\n"
        f"- {language_line}"
    )


def _build_fresh_call_greeting_instructions(
    from_number: str, workspace: str | None, agent_name: str = "bob"
) -> str:
    caller_identity = (
        _lookup_caller_identity(from_number, workspace) if from_number else None
    )
    self_identity = f"You are {agent_name.capitalize()}. "
    if caller_identity:
        spoken_name = caller_identity.preferred_spoken_name
        canonical_name = caller_identity.canonical_name
        if spoken_name == canonical_name:
            greeting_target = (
                f"The caller is {canonical_name}. Greet them by name in one short sentence, "
                f"for example 'Hi {spoken_name}' or 'Hey {spoken_name}, what's up?'. "
            )
        else:
            greeting_target = (
                f"The caller is {canonical_name}. On voice calls, greet them using '{spoken_name}', "
                f"not their full name, in one short sentence, for example 'Hi {spoken_name}' "
                f"or 'Hey {spoken_name}, what's up?'. "
            )
        language_hint = (
            f"Greet in {caller_identity.call_language}. "
            if caller_identity.call_language
            else ""
        )
        return (
            self_identity
            + greeting_target
            + language_hint
            + "Do NOT say 'thanks for calling' or use other stock phone greetings. "
            "Then stop and wait for them to speak."
        )

    return (
        self_identity
        + "A fresh inbound phone call has just connected and the caller is unknown. "
        f"Say 'Hello, this is {agent_name.capitalize()}. Who am I speaking to?' "
        "Do NOT say 'thanks for calling' or use other stock phone greetings. "
        "Then stop and wait for them to answer."
    )


def _build_standup_instructions_guidance() -> str:
    """Return a guidance block prepended to the main instructions for standup calls.

    Unlike ``_build_standup_call_instructions`` (which only controls the model's
    first-turn initial response), this block lives in the permanent ``instructions``
    field and guides the model for the ENTIRE conversation. It is the main fix for
    two UX regressions:

    1. **Subagent deference on brief-answerable questions**: The generic subagent
       instructions say "use the subagent tool" for recent-activity queries. During
       a standup call the brief already contains that data — the model should answer
       from context first, not reach for a live lookup.

    2. **Stale queue references**: The model's session knowledge may include tweet
       drafts or deferred tasks that have already been resolved between brief
       generation and the call.
    """
    return (
        "STANDUP CALL GUIDANCE:\n"
        "- A pre-generated standup brief is loaded in your instructions below. "
        "The brief contains the latest blockers, active work, and recent highlights "
        "as of ~30 minutes before the call.\n"
        "- When Erik asks follow-up questions about items in the brief (including "
        "'what else happened?', 'tell me more about X', or 'what's blocking Y'), "
        "answer from the brief content first. Do NOT use the subagent tool for "
        "routine recap or elaboration on items already covered by the brief.\n"
        "- The subagent tool is for genuinely novel questions only: a specific PR "
        "not mentioned, a task status change since the brief was prepared, or "
        "something the brief is genuinely silent on.\n"
        "- Queue state (pending tweets, deferred tasks) in the brief may have "
        "changed since generation. Frame pending items as 'as of ~30 minutes ago' "
        "rather than definitely pending. Do NOT volunteer stale queue state that "
        "is not mentioned in the brief.\n"
    )


# Stale threshold for the pre-computed voice digest (4 hours).  A digest older
# than this is too stale to be useful and may mislead the model.
_VOICE_DIGEST_MAX_AGE_SECONDS = 4 * 3600


def _load_voice_digest(workspace: str | None) -> str | None:
    """Load the pre-computed voice activity digest if one exists and is fresh.

    The digest is written by ``scripts/voice-digest-precompute.py`` (run at
    autonomous session start via autonomous-fanout.sh).  It contains recent
    session outcome summaries sourced from journal ``**Outcome**`` lines — the
    same high-level signal used by the outbound standup brief path.

    Returns None when the workspace is unknown, the file is absent, or the
    file is older than ``_VOICE_DIGEST_MAX_AGE_SECONDS``.
    """
    if not workspace:
        return None
    digest_path = Path(workspace) / "state" / "voice-digest.md"
    try:
        if not digest_path.exists():
            return None
        age = time.time() - digest_path.stat().st_mtime
        if age > _VOICE_DIGEST_MAX_AGE_SECONDS:
            logger.debug(
                "voice digest stale (%.0fs > %ds) — skipping",
                age,
                _VOICE_DIGEST_MAX_AGE_SECONDS,
            )
            return None
        return digest_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("failed to read voice digest %s: %s", digest_path, exc)
        return None


def _prepend_activity_digest(digest_text: str, instructions: str) -> str:
    """Prepend the voice digest to call instructions with guidance to use it.

    Mirrors ``_build_standup_instructions_guidance()`` for the inbound case:
    the model is told to answer recap questions from the pre-loaded digest
    instead of spawning a subagent.
    """
    guidance = (
        "ACTIVITY DIGEST (pre-computed — do not read aloud):\n"
        "- A compact summary of recent work is loaded below. "
        "Use it to answer 'what did you do today / in the last 12 hours?' "
        "without spawning a subagent.\n"
        "- Treat this as of the 'Generated at' timestamp shown in the digest; "
        "sessions that started after that point are not included.\n"
        "- For questions about specific task status or anything genuinely absent "
        "from the digest, you may use the subagent tool for a targeted lookup.\n\n"
        f"{digest_text.strip()}\n"
    )
    return guidance + "\n\n" + instructions


def _build_standup_call_instructions(brief_text: str) -> str:
    """Build initial response instructions for a standup call with a pre-generated brief.

    Guides the voice model to deliver an outbound standup call that sounds
    deliberate, prepared, and confident — not like a pre-recorded message.

    Critical design constraints:
    - The brief is pre-generated and loaded into context as part of ``instructions``.
      After delivery, follow-up questions should be answered from the brief content
      first, NOT by spawning a subagent. This avoids the failure mode where the
      model says "let me check that" for a routine recap that's already in its own
      context window.
    - Stale queue state (pending tweets, deferred tasks) must not be volunteered
      unless the brief explicitly mentions them. The model should supplement the
      brief with its own session knowledge only when the brief is silent on a topic
      AND the knowledge is clearly current.
    """
    return (
        "This is an outbound daily standup call you initiated to Erik. "
        "You are the one calling him, not the other way around — own the opening.\n\n"
        "1. **Greet first** — say 'Good morning Erik' or 'Hi Erik' in one short sentence. "
        "Do NOT start with filler like 'Hey...' or 'So...'. "
        "Do NOT say 'thanks for taking my call' or 'thanks for picking up'.\n\n"
        "2. **Deliver the brief** — read the brief below naturally. Do NOT announce "
        "'Here is the standup brief' or 'Let me read you the brief'. "
        "Just lead into it conversationally. "
        "Pause briefly between items. Do not rush.\n\n"
        "3. **Sound deliberate and confident** — speak at a measured pace. "
        "You prepared this brief for a reason; deliver it like you mean it. "
        "If something is blocking progress, say so plainly. "
        "If something went well, acknowledge it.\n\n"
        "4. **Handle follow-up questions from the brief** — when Erik asks a "
        "follow-up like 'what else happened?' or asks for more detail on something "
        "in the brief, answer from the brief content already loaded in your "
        "instructions. The brief bullets (blockers, active_tasks, recent_highlights) "
        "are available. DO NOT use the subagent tool for routine recap or "
        "elaboration on items already covered by the brief.\n\n"
        "5. **Subagent is for genuinely novel questions only** — use the subagent "
        "tool ONLY when Erik asks about something clearly outside the brief: a "
        "specific PR that was not mentioned, a task status change since the brief "
        "was prepared, or a question that the brief is genuinely silent on. For "
        "routine 'tell me more about X' where X is in the brief, answer from the "
        "brief rather than reaching for a subagent.\n\n"
        "6. **Stale content awareness** — the brief is generated ~30 minutes before "
        "the call. Queue state (pending tweets, deferred tasks) may have changed. "
        "If the brief mentions pending items, frame them as 'as of ~30 minutes "
        "ago' rather than asserting they're still pending. Do NOT volunteer stale "
        "queue state that is not in the brief even if your session knowledge "
        "suggests those items once existed.\n\n"
        "7. **Hand off** — after the brief, say something like "
        "'That's what I've got — what do you think?' or 'Over to you — any questions?' "
        "Then stop and wait for Erik to respond.\n\n"
        f"--- Standup brief ---\n\n{brief_text}"
    )


def _append_transcript_turn(
    transcript: list[TranscriptTurn],
    role: str,
    text: str,
    *,
    item_id: str | None = None,
    allow_continuation: bool = True,
) -> None:
    """Add a transcript turn, replacing the last entry when it is a partial of the same utterance.

    Some ASR providers (e.g. xAI/Grok) fire the completed event multiple times
    for the same utterance, each time with the full accumulated text so far.

    When item_id is provided (preferred): replace if and only if the last same-role entry
    carries the same item_id.  This is exact and avoids the false-positive where a new
    utterance happens to begin with the same text as the previous one.

    When item_id is absent (fallback): replace if the new text is a prefix-extension of
    the last same-role entry AND the last entry also has no item_id.  Providers that do
    not expose item_id in the event still benefit from deduplication; the false-positive
    risk (two distinct utterances sharing a prefix, both without item_id) is inherent
    to the heuristic but is significantly narrowed by requiring both sides to be
    id-less — an entry already anchored to an item_id can never be silently replaced
    by a no-id event.

    Set allow_continuation=False (e.g. for assistant turns) to always append as a new
    entry, preventing the prefix heuristic from treating two distinct final transcripts
    as continuations of the same utterance.
    """
    cleaned = text.strip()
    if not cleaned:
        return

    # Fast path: exact item_id match (authoritative provider correlation key).
    # Replace only when the new text is at least as long as the stored one —
    # a shorter retransmission (e.g. a provider-side correction) is silently
    # ignored so we never truncate a longer partial already in the transcript.
    if (
        allow_continuation
        and item_id is not None
        and transcript
        and transcript[-1].role == role
        and transcript[-1].item_id == item_id
    ):
        if len(cleaned) >= len(transcript[-1].text):
            transcript[-1] = TranscriptTurn(role=role, text=cleaned, item_id=item_id)
        return  # same item_id — never append a second entry regardless

    # Prefix heuristic fallback: only when BOTH the current event and the
    # stored entry have no item_id.  If the stored entry has item_id="A"
    # and a new event arrives with item_id=None, the new event is a
    # different utterance even if its text happens to extend the prior one.
    is_continuation = allow_continuation and (
        transcript
        and transcript[-1].role == role
        and item_id is None
        and transcript[-1].item_id is None
        and cleaned.startswith(transcript[-1].text)
    )

    if is_continuation:
        transcript[-1] = TranscriptTurn(role=role, text=cleaned, item_id=item_id)
    else:
        transcript.append(TranscriptTurn(role=role, text=cleaned, item_id=item_id))


def _normalize_transcript_text(text: str) -> str:
    return " ".join(text.split())


def _user_requested_hangup(text: str) -> bool:
    return bool(_USER_HANGUP_INTENT_RE.search(_normalize_transcript_text(text)))


def _assistant_committed_hangup(text: str) -> bool:
    normalized = _normalize_transcript_text(text)
    if _ASSISTANT_HANGUP_DISQUALIFIERS_RE.search(normalized):
        return False
    return bool(_ASSISTANT_HANGUP_COMMIT_RE.search(normalized))


def _recent_user_requested_hangup(
    transcript: list[TranscriptTurn], *, max_user_turns: int = 3
) -> bool:
    seen_user_turns = 0
    for turn in reversed(transcript):
        if turn.role != "user":
            continue
        seen_user_turns += 1
        if _user_requested_hangup(turn.text):
            return True
        if seen_user_turns >= max_user_turns:
            break
    return False


def _should_trigger_hangup_transcript_fallback(
    transcript: list[TranscriptTurn], assistant_text: str
) -> bool:
    return _assistant_committed_hangup(
        assistant_text
    ) and _recent_user_requested_hangup(transcript)


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
_VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

# Friendly provider descriptions for truthful runtime self-reporting. The
# realtime voice model otherwise confabulates an unrelated identity (observed
# 2026-06-05: claiming "Claude 3.5 Sonnet" on a Grok-powered call) when a caller
# asks what is powering the conversation.
_PROVIDER_DISPLAY = {
    _PROVIDER_OPENAI: "OpenAI's realtime voice API",
    _PROVIDER_GROK: "xAI's Grok realtime voice API",
}


def _build_runtime_identity_instructions(provider: str, model: str | None) -> str:
    """Return a truthful runtime-identity block for the active voice provider.

    The realtime model has no inherent knowledge of which provider/model is
    serving the live call, so when asked "what model are you running on?" it
    confabulates. This block states the ground truth so the model answers
    honestly instead of guessing an unrelated vendor.
    """
    display = _PROVIDER_DISPLAY.get(provider, f"the {provider} realtime voice API")
    model_clause = f" (model: {model})" if model else ""
    return (
        "RUNTIME IDENTITY:\n"
        f"- This live voice conversation is served by {display}{model_clause}.\n"
        "- If the caller asks what model or provider is powering this call, answer "
        "truthfully with that. Do NOT claim to be Claude, GPT, or any other model "
        "unless it matches the provider above.\n"
        "- Your text-based gptme persona and code-lookup subagent may run on a "
        "different model; only describe the live voice provider above when asked "
        "about the current call. If you are genuinely unsure, say so rather than "
        "guessing a vendor."
    )


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
        reasoning_effort: str | None = "low",
        voice: str | None = None,
        output_speed: float | None = None,
        enable_browser_transport: bool = False,
    ):
        self.host = host
        self.port = port
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.voice = voice
        self.output_speed = output_speed
        self.enable_browser_transport = enable_browser_transport
        # G.711 μ-law passthrough on the OpenAI Twilio path. Only takes effect
        # for provider=openai; Grok is unaffected because its API requires PCM.
        self.openai_g711_passthrough = (
            (_get_config_env("GPTME_VOICE_OPENAI_G711_PASSTHROUGH") or "").lower()
            in ("1", "true", "yes")
        ) and provider == _PROVIDER_OPENAI
        if provider == _PROVIDER_GROK:
            self._api_key = _get_xai_api_key()
        else:
            self._api_key = openai_api_key or _get_openai_api_key()
        self.workspace = workspace or _detect_agent_repo()
        self._agent_name = (
            _get_config_env("GPTME_VOICE_AGENT_NAME")
            or _get_config_env("AGENT_NAME")
            or "bob"
        )
        # Prepend the stable persona name and truthful runtime identity so neither
        # gets lost when the compact voice prompt truncates personality files.
        self._instructions = (
            f"IDENTITY: You are {self._agent_name.capitalize()}. "
            "Never claim to be another agent.\n\n"
            + _build_runtime_identity_instructions(self.provider, self.model)
            + "\n\n"
            + _load_project_instructions(self.workspace)
        )
        self.resume_window_seconds = int(
            _get_config_env("GPTME_VOICE_RESUME_WINDOW_SECONDS")
            or _DEFAULT_RESUME_WINDOW_SECONDS
        )
        self.post_call_delay_seconds = int(
            _get_config_env("GPTME_VOICE_POST_CALL_DELAY_SECONDS")
            or self.resume_window_seconds
        )
        self.post_call_command = _get_config_env("GPTME_VOICE_POST_CALL_COMMAND")
        self.gptme_server_url = _get_config_env("GPTME_VOICE_GPTME_SERVER_URL") or ""
        self.gptme_server_key = _get_config_env("GPTME_VOICE_GPTME_SERVER_KEY") or ""
        self.state_dir = Path(
            _get_config_env("GPTME_VOICE_STATE_DIR") or _DEFAULT_STATE_DIR
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Cross-agent handoff writer (optional — only active when GPTME_VOICE_HANDOFF_DIR set)
        handoff_dir_env = _get_config_env("GPTME_VOICE_HANDOFF_DIR")
        handoff_agent_name = (
            _get_config_env("GPTME_VOICE_AGENT_NAME") or "bob"
        ).lower()
        handoff_secret_env = _get_config_env("GPTME_VOICE_HANDOFF_SECRET")
        handoff_agents_env = _get_config_env("GPTME_VOICE_HANDOFF_AGENTS")
        # Comma-separated list of agents the running server can hand off to.
        # Defaults to the known agents minus the current protocol identity.
        _default_agents = [
            a for a in ["alice", "gordon", "sven", "bob"] if a != handoff_agent_name
        ]
        self._available_agents: list[str] = (
            [a.strip() for a in handoff_agents_env.split(",") if a.strip()]
            if handoff_agents_env
            else _default_agents
        )
        # Optional physical body (BobBrain): registers capability-gated
        # body_* tools when GPTME_VOICE_BODY_URL is set.
        self.body_adapter = body_adapter_from_env()
        if self.body_adapter is not None:
            logger.info(
                "Body adapter configured: %s (capabilities: %s)",
                self.body_adapter.name,
                sorted(self.body_adapter.capabilities) or "none",
            )
        if handoff_dir_env:
            if not handoff_secret_env:
                logger.warning(
                    "GPTME_VOICE_HANDOFF_SECRET not set while GPTME_VOICE_HANDOFF_DIR is "
                    "configured — using insecure fallback. Set GPTME_VOICE_HANDOFF_SECRET "
                    "to a strong random value in production."
                )
            handoff_secret = (handoff_secret_env or "dev-only-secret").encode("utf-8")
            self._handoff_writer: HandoffWriter | None = HandoffWriter(
                Path(handoff_dir_env),
                from_agent=handoff_agent_name,
                secret=handoff_secret,
            )
            logger.info(
                "Handoff enabled: from_agent=%s, dir=%s",
                handoff_agent_name,
                handoff_dir_env,
            )
        else:
            self._handoff_writer = None

        # Active connections: call_sid -> (twilio_ws, realtime_client)
        self._connections: dict[str, tuple] = {}
        self._pending_post_calls: dict[str, str] = {}
        self._pending_archive_records: dict[str, list[Path]] = {}
        # Pre-warmed realtime connections: from_number -> (client, created_at)
        # Keyed by from_number, claimed and discarded when the Twilio stream starts.
        self._prewarm_sessions: dict[str, tuple[OpenAIRealtimeClient, float]] = {}
        # In-flight pre-warm tasks by from_number, so _claim_prewarm can await
        # a pre-warm that hasn't finished connecting yet instead of racing it
        # into a duplicate cold session (observed 2026-08-27: Twilio's "start"
        # arrived before "Pre-warm ready", leaving an orphaned provider session).
        self._prewarm_tasks: dict[str, asyncio.Task] = {}
        # Numbers whose pre-warm has connected and is finalizing (consuming
        # resume state). Cancelling in this phase could lose resume context,
        # so _claim_prewarm briefly extends its wait instead.
        self._prewarm_connected: set[str] = set()
        # Max seconds a pre-warm session is kept before being discarded
        self._prewarm_ttl_seconds = 30
        # Call-scoped body-tool grants minted by the signed /incoming webhook.
        # The /twilio WebSocket is unauthenticated; customParameters.from_number
        # is attacker-controlled, so body tools must not key off it. Token ->
        # (from_number, call_sid, expires_monotonic). CallSid is bound from the
        # signed webhook and is not copied into TwiML, so a stolen grant cannot
        # be replayed onto a different call. Consume does not pop: Twilio
        # reconnects resend the same start customParameters, and popping would
        # leave an airborne vehicle with no body_stop. Mint TTL covers the
        # /incoming-to-first-start window; first successful consume pins the
        # grant (expires=inf) until Twilio stop, hangup, idle-disconnect, or
        # process restart. Do not revoke on websocket drop itself — that is
        # the reconnect path — but schedule an idle revoke so a call that
        # never sends ``stop`` cannot leave a live bearer token forever.
        self._twilio_body_grants: dict[str, tuple[str, str, float]] = {}
        self._twilio_body_grant_ttl_s = 120.0
        self._twilio_body_grant_idle_s = 90.0
        self._twilio_grant_idle_tasks: dict[str, asyncio.Task[None]] = {}

        routes = [
            Route("/", self.health_check, methods=["GET"]),
            Route("/incoming", self.handle_incoming_call, methods=["POST"]),
            WebSocketRoute("/twilio", self.handle_twilio_websocket),
            WebSocketRoute("/local", self.handle_local_websocket),
        ]
        if self.enable_browser_transport:
            routes.append(WebSocketRoute("/voice", self.handle_browser_websocket))
            routes.append(Route("/browser", self.serve_browser_client, methods=["GET"]))

        self.app = Starlette(routes=routes, lifespan=self._lifespan)

    @contextlib.asynccontextmanager
    async def _lifespan(self, _app):
        try:
            yield
        finally:
            await self._cancel_all_twilio_body_grant_idle_revokes()
            if self.body_adapter is not None:
                await self.body_adapter.close()

    def _twilio_body_caller_allowed(self, caller_id: str | None) -> bool:
        """Twilio body tools require an explicit caller allowlist match.

        Twilio's request signature proves the webhook came from Twilio, not
        that the human on the line is authorized to fly a vehicle. Fail
        closed: no allowlist, or a caller not on it, means no motion tools.
        """
        raw = _get_config_env("TWILIO_CALLER_ALLOWLIST")
        if not raw or not caller_id:
            return False
        allowlist = {n.strip() for n in raw.split(",") if n.strip()}
        return caller_id in allowlist

    def _expire_twilio_body_grants(self) -> None:
        now = time.monotonic()
        expired = [
            token
            for token, (*_, expires) in self._twilio_body_grants.items()
            if now > expires
        ]
        for token in expired:
            self._twilio_body_grants.pop(token, None)

    def _mint_twilio_body_grant(self, from_number: str, call_sid: str) -> str:
        """Mint a call-scoped token proving /incoming authorized this caller.

        Both From and CallSid must be non-empty. An empty CallSid would skip
        the consume-time bind and let a stolen grant replay onto any start
        event that presents the same From.
        """
        from_number = from_number.strip()
        call_sid = call_sid.strip()
        if not from_number or not call_sid:
            logger.warning("Refusing Twilio body grant with empty From or CallSid")
            return ""
        self._expire_twilio_body_grants()
        token = secrets.token_urlsafe(32)
        self._twilio_body_grants[token] = (
            from_number,
            call_sid,
            time.monotonic() + self._twilio_body_grant_ttl_s,
        )
        return token

    def _consume_twilio_body_grant(self, token: str | None) -> tuple[str, str] | None:
        """Return ``(from_number, call_sid)`` bound to a live grant, or None.

        Call-scoped, not single-use: Twilio reconnects resend the same
        ``start`` customParameters, so popping the token would leave an
        airborne vehicle with no ``body_stop``. Replay onto a different
        call is still fail-closed via CallSid binding. Unknown, expired,
        and revoked tokens fail closed. Mint TTL covers first start;
        a successful consume pins the grant until Twilio ``stop`` or hangup.
        """
        self._expire_twilio_body_grants()
        if not token:
            return None
        entry = self._twilio_body_grants.get(token)
        if entry is None:
            return None
        from_number, call_sid, expires = entry
        if not from_number or not call_sid:
            # Empty CallSid skips the start-event bind; drop the grant.
            self._twilio_body_grants.pop(token, None)
            return None
        if time.monotonic() > expires:
            self._twilio_body_grants.pop(token, None)
            return None
        # Pin after first start so a long call's later reconnect still
        # has body_stop. Unused grants still expire at mint TTL.
        if expires != float("inf"):
            self._twilio_body_grants[token] = (from_number, call_sid, float("inf"))
        return from_number, call_sid

    def _revoke_twilio_body_grants_for_call(self, call_sid: str | None) -> None:
        """Drop grants bound to a CallSid. Twilio ``stop`` is the real call end."""
        if not call_sid:
            return
        self._cancel_twilio_body_grant_idle_revoke(call_sid)
        to_drop = [
            token
            for token, (_from, sid, _exp) in self._twilio_body_grants.items()
            if sid == call_sid
        ]
        for token in to_drop:
            self._twilio_body_grants.pop(token, None)

    def _cancel_twilio_body_grant_idle_revoke(self, call_sid: str | None) -> None:
        """Cancel a pending idle-disconnect revoke for this CallSid."""
        if not call_sid:
            return
        task = self._twilio_grant_idle_tasks.pop(call_sid, None)
        if task is None or task.done():
            return
        # The idle task itself calls revoke; don't cancel the running task.
        try:
            if task is asyncio.current_task():
                return
        except RuntimeError:
            pass
        task.cancel()

    async def _cancel_all_twilio_body_grant_idle_revokes(self) -> None:
        tasks = [
            self._twilio_grant_idle_tasks.pop(sid)
            for sid in list(self._twilio_grant_idle_tasks)
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_twilio_body_grant_idle_revoke(self, call_sid: str | None) -> None:
        """Revoke pinned grants if this CallSid does not reconnect.

        Twilio Media Stream reconnects drop the websocket without ``stop``.
        Revoking in ``finally`` would take ``body_stop`` away mid-flight.
        Waiting ``_twilio_body_grant_idle_s`` then revoking iff the CallSid
        is still absent from ``_connections`` bounds the leak when neither
        ``stop`` nor hangup arrives.
        """
        if not call_sid:
            return
        if not any(
            sid == call_sid for _from, sid, _exp in self._twilio_body_grants.values()
        ):
            return
        self._cancel_twilio_body_grant_idle_revoke(call_sid)
        delay = self._twilio_body_grant_idle_s

        async def _idle_revoke() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if call_sid in self._connections:
                return
            logger.info(
                "Revoking Twilio body grants for %s after idle disconnect",
                call_sid,
            )
            self._revoke_twilio_body_grants_for_call(call_sid)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._twilio_grant_idle_tasks[call_sid] = loop.create_task(_idle_revoke())

    def _body_adapter_for_websocket(
        self, websocket, *, transport: str, caller_id: str | None = None
    ):
        """Expose motion tools only on loopback or allowlisted Twilio callers."""
        if self.body_adapter is None:
            return None
        if transport == "twilio":
            if self._twilio_body_caller_allowed(caller_id):
                return self.body_adapter
            logger.warning(
                "Body tools disabled for Twilio caller %s (not on TWILIO_CALLER_ALLOWLIST)",
                caller_id or "unknown",
            )
            return None
        host = _websocket_peer_host(websocket)
        if host in {"127.0.0.1", "::1", "localhost"}:
            return self.body_adapter
        logger.warning(
            "Body tools disabled for unauthenticated %s client %s",
            transport,
            host or "unknown",
        )
        return None

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

    def _record_payload(
        self, record: RecentCallRecord, *, include_pending_state: bool = False
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "caller_id": record.caller_id,
            "source": record.source,
            "ended_at": record.ended_at,
            "transcript": [asdict(turn) for turn in record.transcript],
            "metadata": record.metadata,
        }
        if record.subagent_timings:
            payload["subagent_timings"] = record.subagent_timings
        if include_pending_state and record.archive_record_paths:
            payload["archive_record_paths"] = record.archive_record_paths
        if include_pending_state and record.pending_post_call_unit:
            payload["pending_post_call_unit"] = record.pending_post_call_unit
        return payload

    def _write_call_record(
        self,
        path: Path,
        record: RecentCallRecord,
        *,
        include_pending_state: bool = False,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                self._record_payload(
                    record, include_pending_state=include_pending_state
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return path

    def _save_recent_call(self, record: RecentCallRecord) -> Path:
        return self._write_call_record(
            self._recent_call_path(record.caller_id),
            record,
            include_pending_state=True,
        )

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
                    TranscriptTurn(
                        role=item["role"],
                        text=item["text"],
                        item_id=item.get("item_id"),
                    )
                    for item in payload.get("transcript", [])
                    if item.get("role") and item.get("text")
                ]
                raw_timings = payload.get("subagent_timings") or []
                subagent_timings = [
                    dict(item) for item in raw_timings if isinstance(item, dict)
                ]
                raw_archive_paths = payload.get("archive_record_paths") or []
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
                    archive_record_paths=[
                        str(path)
                        for path in raw_archive_paths
                        if isinstance(path, str) and path.strip()
                    ],
                    pending_post_call_unit=(
                        payload.get("pending_post_call_unit")
                        if isinstance(payload.get("pending_post_call_unit"), str)
                        and payload.get("pending_post_call_unit")
                        else None
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load recent call state from %s: %s", path, exc
                )

        return None

    def _dedupe_record_paths(self, record_paths: list[Path]) -> list[Path]:
        return list(dict.fromkeys(record_paths))

    def _restore_archive_record_paths(self, raw_paths: list[str]) -> list[Path]:
        restored_paths: list[Path] = []
        for raw_path in raw_paths:
            path = Path(raw_path)
            if path.exists():
                restored_paths.append(path)
        return self._dedupe_record_paths(restored_paths)

    def _build_post_call_unit_name(
        self, caller_id: str, record_paths: list[Path]
    ) -> str | None:
        deduped_record_paths = self._dedupe_record_paths(record_paths)
        if not deduped_record_paths:
            return None

        digest = hashlib.sha256()
        digest.update(caller_id.encode("utf-8"))
        for record_path in deduped_record_paths:
            digest.update(b"\0")
            digest.update(str(record_path).encode("utf-8"))
        return f"gptme-voice-post-call-{digest.hexdigest()[:12]}"

    def _cancel_post_call_schedule(self, unit_name: str | None) -> None:
        if not unit_name:
            return

        units = (f"{unit_name}.timer", f"{unit_name}.service")
        for action in ("stop", "reset-failed"):
            for unit in units:
                result = subprocess.run(
                    ["systemctl", "--user", action, unit],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    continue

                stderr = (result.stderr or "").strip().lower()
                if "not loaded" in stderr or "not found" in stderr:
                    continue

                logger.warning(
                    "Failed to %s pending post-call unit %s: exit=%s stderr=%s",
                    action,
                    unit,
                    result.returncode,
                    (result.stderr or "").strip(),
                )

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

    async def _build_session_bootstrap(
        self,
        *,
        caller_id: str | None,
        from_number: str = "",
        handoff_id: str | None = None,
        standup_brief: str | None = None,
        consume_recent: bool = True,
    ) -> SessionBootstrap:
        instructions = self._instructions
        if from_number:
            instructions = _build_caller_instructions(
                instructions, from_number, self.workspace
            )

        handoff_resume_context = self._consume_handoff_bootstrap(handoff_id)
        if handoff_resume_context:
            return SessionBootstrap(
                instructions=f"{handoff_resume_context}\n\n{instructions}",
                should_greet_first=False,
            )

        # Inject the pre-computed voice activity digest for inbound calls when no
        # explicit standup_brief was provided.  This ensures Erik's "what did you
        # do today?" question is answered from the digest rather than a live
        # subagent lookup (which timed out on 2026-07-28).  Only loaded when the
        # digest is fresh (< _VOICE_DIGEST_MAX_AGE_SECONDS).
        #
        # Operator-only: external callers (no `- Call role: operator` in their
        # people file, or unknown numbers) must not get internal work status —
        # the digest both leaks internals and steers the model into ops-speak
        # (Philip's first call was greeted with subagent status, 2026-08-27).
        # Calls with no from_number (local/browser transport) keep the digest.
        caller_is_operator = True
        if from_number:
            identity = _lookup_caller_identity(from_number, self.workspace)
            caller_is_operator = bool(identity and identity.is_operator)
        activity_digest = (
            _load_voice_digest(self.workspace) if caller_is_operator else None
        )
        if activity_digest and not standup_brief:
            instructions = _prepend_activity_digest(activity_digest, instructions)

        # standup_brief takes priority over recent-call resume: an explicit outbound
        # standup should always deliver the brief, not silently resume a prior session.
        if standup_brief:
            return SessionBootstrap(
                instructions=(
                    _build_standup_instructions_guidance()
                    + "\n\n"
                    + standup_brief
                    + "\n\n"
                    + instructions
                ),
                should_greet_first=True,
                initial_response_instructions=_build_standup_call_instructions(
                    standup_brief
                ),
            )

        recent_call = await self._consume_recent_call(caller_id, consume=consume_recent)
        if recent_call:
            return SessionBootstrap(
                instructions=_build_resume_instructions(
                    instructions,
                    recent_call,
                    self.resume_window_seconds,
                ),
                should_greet_first=False,
            )

        return SessionBootstrap(
            instructions=instructions,
            should_greet_first=True,
            initial_response_instructions=_build_fresh_call_greeting_instructions(
                from_number,
                self.workspace,
                self._agent_name,
            ),
        )

    def _evict_stale_prewarms(self) -> None:
        """Discard pre-warm entries that exceeded TTL without being claimed."""
        now = time.monotonic()
        stale = [
            num
            for num, (_, created_at) in self._prewarm_sessions.items()
            if now - created_at > self._prewarm_ttl_seconds
        ]
        for num in stale:
            client, _ = self._prewarm_sessions.pop(num)
            logger.info("Evicting stale pre-warm for %s", num)
            asyncio.create_task(self._disconnect_realtime_client(client))

    async def _prewarm_for_inbound(self, from_number: str) -> None:
        """Pre-connect to the realtime API while Twilio is setting up the media stream.

        Called as a background task from handle_incoming_call so the provider
        WebSocket and session handshake complete before the Twilio stream's
        ``start`` event arrives.  The client is stored with
        ``hold_initial_response=True`` so no greeting is sent before the
        call-side WebSocket is ready; activate_session() releases it.
        """
        self._evict_stale_prewarms()
        client: OpenAIRealtimeClient | None = None
        try:
            # Peek at the resume record (consume_recent=False) so the on-disk
            # state file is NOT deleted until connect() succeeds.  If connect()
            # raises, the file stays intact and the cold-path _build_session_bootstrap
            # can still resume the caller normally.
            bootstrap = await self._build_session_bootstrap(
                caller_id=from_number,
                from_number=from_number,
                consume_recent=False,
            )
            session_cfg = self._build_session_config(
                instructions=bootstrap.instructions,
                initial_response_instructions=(
                    bootstrap.initial_response_instructions
                    if bootstrap.should_greet_first
                    else ""
                ),
                include_body_tools=self._twilio_body_caller_allowed(from_number),
            )
            client = self._make_client(session_cfg, hold_initial_response=True)
            await client.connect()
            # Mark the post-connect phase: from here on the task will consume
            # the caller's resume state, so _claim_prewarm must not cancel it
            # (cancellation mid-consume could delete the resume file without a
            # session to show for it — losing the caller's resume context).
            self._prewarm_connected.add(from_number)
            # connect() succeeded — now safely consume the resume state so a
            # cold-path fallback won't re-inject the same transcript.
            await self._consume_recent_call(from_number)
            self._prewarm_sessions[from_number] = (client, time.monotonic())
            # Guarantee eviction even if no further call ever arrives
            # (_evict_stale_prewarms otherwise only runs on the next inbound).
            asyncio.get_running_loop().call_later(
                self._prewarm_ttl_seconds + 1, self._evict_stale_prewarms
            )
            logger.info("Pre-warm ready for %s", from_number)
        except asyncio.CancelledError:
            # Claim timed out and cancelled us (or the server is shutting
            # down): close the half-open provider session instead of leaking it.
            logger.info("Pre-warm cancelled for %s", from_number)
            if client is not None:
                asyncio.create_task(self._disconnect_realtime_client(client))
            raise
        except Exception as exc:
            logger.warning("Pre-warm failed for %s: %s", from_number, exc)
            # connect() (or _consume_recent_call) failed after the client was
            # created: close the half-open provider session instead of leaking
            # one connection per failed pre-warm attempt.
            if client is not None:
                asyncio.create_task(self._disconnect_realtime_client(client))
        finally:
            self._prewarm_connected.discard(from_number)

    def _register_prewarm_task(self, from_number: str) -> None:
        """Start a pre-warm task for from_number and track it for claiming."""
        task = asyncio.create_task(self._prewarm_for_inbound(from_number))
        self._prewarm_tasks[from_number] = task

        def _cleanup(done: asyncio.Task, num: str = from_number) -> None:
            if self._prewarm_tasks.get(num) is done:
                self._prewarm_tasks.pop(num, None)

        task.add_done_callback(_cleanup)

    async def _claim_prewarm(
        self,
        from_number: str,
        *,
        wait_seconds: float = 2.0,
        finalize_wait_seconds: float = 5.0,
    ) -> OpenAIRealtimeClient | None:
        """Claim and remove a pre-warmed session for from_number if still fresh.

        If the pre-warm is still connecting, wait up to *wait_seconds* for it —
        a near-ready pre-warm beats starting a cold session from scratch, and
        racing past it would leave an orphaned provider session (the doubled
        "Session created" observed on 2026-08-27).  On timeout the in-flight
        task is cancelled so the cold path doesn't end up with a twin.
        """
        entry = self._prewarm_sessions.pop(from_number, None)
        if entry is None:
            task = self._prewarm_tasks.get(from_number)
            if task is not None and not task.done():
                logger.info(
                    "Pre-warm for %s still connecting — waiting up to %.1fs",
                    from_number,
                    wait_seconds,
                )
                try:
                    await asyncio.wait_for(asyncio.shield(task), wait_seconds)
                except asyncio.TimeoutError:
                    # The task may have completed (storing its session) between
                    # the timeout firing and now — never discard that: the
                    # completed pre-warm holds the caller's (already consumed)
                    # resume context, so dropping it would greet fresh instead
                    # of resuming. Claim it.
                    entry = self._prewarm_sessions.pop(from_number, None)
                    if entry is not None:
                        logger.info(
                            "Pre-warm for %s completed at the timeout boundary — claiming it",
                            from_number,
                        )
                        return entry[0]
                    if from_number in self._prewarm_connected:
                        # Connected and finalizing (consuming resume state —
                        # local file I/O, fast in practice). A post-connect
                        # task is NEVER cancelled: cancellation could
                        # interrupt _consume_recent_call after the resume
                        # file's unlink but before the session is stored,
                        # destroying the caller's resume context. Extend the
                        # wait instead; if it still isn't done, fall back
                        # cold WITHOUT cancelling — the finished session is
                        # disconnected by TTL eviction.
                        logger.info(
                            "Pre-warm for %s is finalizing — extending wait",
                            from_number,
                        )
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(task), finalize_wait_seconds
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Pre-warm for %s still finalizing after "
                                "%.1fs — using cold path without cancelling",
                                from_number,
                                finalize_wait_seconds,
                            )
                            return None
                        except Exception:
                            pass  # failure already logged by the task
                        entry = self._prewarm_sessions.pop(from_number, None)
                        if entry is not None:
                            logger.info(
                                "Claimed pre-warm for %s after finalize wait",
                                from_number,
                            )
                            return entry[0]
                        return None  # finalize failed — cold path
                    logger.info(
                        "Pre-warm for %s not ready in time — cancelling, using cold path",
                        from_number,
                    )
                    # Pre-connect cancellation is safe: the peek design means
                    # the resume state has not been consumed yet.
                    task.cancel()
                    # Defensive: reap anything stored despite the paths above.
                    self._reap_prewarm_entry(from_number)
                    return None
                except Exception:
                    pass  # failure already logged by _prewarm_for_inbound
                entry = self._prewarm_sessions.pop(from_number, None)
        if entry is None:
            return None
        client, created_at = entry
        age = time.monotonic() - created_at
        if age > self._prewarm_ttl_seconds:
            logger.info(
                "Discarding stale pre-warm for %s (%.1fs old)", from_number, age
            )
            asyncio.create_task(self._disconnect_realtime_client(client))
            return None
        logger.info("Claimed pre-warm for %s (%.1fs old)", from_number, age)
        return client

    def _reap_prewarm_entry(self, from_number: str) -> None:
        """Remove and disconnect a stored pre-warm session, if one exists."""
        entry = self._prewarm_sessions.pop(from_number, None)
        if entry is not None:
            client, _ = entry
            asyncio.create_task(self._disconnect_realtime_client(client))

    async def _build_session_instructions(
        self,
        *,
        caller_id: str | None,
        from_number: str = "",
        handoff_id: str | None = None,
    ) -> str:
        return (
            await self._build_session_bootstrap(
                caller_id=caller_id,
                from_number=from_number,
                handoff_id=handoff_id,
            )
        ).instructions

    async def _consume_recent_call(
        self, caller_id: str | None, *, consume: bool = True
    ) -> RecentCallRecord | None:
        """Load the most recent call record for *caller_id*.

        When *consume* is True (default) the on-disk state file is deleted,
        any pending post-call schedule is cancelled, and archive record paths
        are restored.  Pass ``consume=False`` to peek at the record without
        side effects — useful when building bootstrap instructions before a
        connect() that may fail, to avoid losing resume context.
        """
        if not caller_id:
            return None

        recent_call = self._load_recent_call(caller_id)
        if not recent_call:
            return None

        age_seconds = time.time() - recent_call.ended_at
        if age_seconds > self.resume_window_seconds:
            return None

        if not consume:
            return recent_call

        pending_unit = (
            self._pending_post_calls.pop(caller_id, None)
            or recent_call.pending_post_call_unit
        )
        if pending_unit:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._cancel_post_call_schedule, pending_unit
            )
            logger.info(
                "Deferred pending post-call follow-up for resumed caller %s", caller_id
            )

        restored_archive_paths = self._restore_archive_record_paths(
            recent_call.archive_record_paths
        )
        if restored_archive_paths:
            self._pending_archive_records[caller_id] = restored_archive_paths
        else:
            self._pending_archive_records.pop(caller_id, None)

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

    async def _run_post_call_command(
        self,
        caller_id: str,
        record_paths: list[Path],
        *,
        delay_seconds: int = 0,
        unit_name: str | None = None,
    ) -> None:
        if not self.post_call_command:
            return
        if not record_paths:
            logger.warning(
                "Ignoring post-call command for %s with no records", caller_id
            )
            return

        argv = shlex.split(self.post_call_command)
        if not argv:
            logger.warning("Ignoring empty GPTME_VOICE_POST_CALL_COMMAND")
            return

        env = os.environ.copy()
        env["GPTME_VOICE_POST_CALL_JSON"] = str(record_paths[0])
        env["GPTME_VOICE_POST_CALL_JSONS"] = json.dumps(
            [str(path) for path in record_paths]
        )
        env["GPTME_VOICE_CALLER_ID"] = caller_id
        if delay_seconds > 0:
            env["GPTME_VOICE_POST_CALL_DELAY_SECONDS"] = str(delay_seconds)
        if unit_name:
            env["GPTME_VOICE_POST_CALL_UNIT_NAME"] = unit_name
        process = await asyncio.create_subprocess_exec(
            *argv,
            *(str(path) for path in record_paths),
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

    async def _schedule_post_call(
        self, caller_id: str, record_paths: list[Path]
    ) -> None:
        existing_unit = self._pending_post_calls.pop(caller_id, None)
        if existing_unit:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._cancel_post_call_schedule, existing_unit
            )

        deduped_record_paths = self._dedupe_record_paths(record_paths)
        if not deduped_record_paths:
            self._pending_archive_records.pop(caller_id, None)
            logger.warning(
                "Ignoring post-call schedule for %s with no records", caller_id
            )
            return

        self._pending_archive_records[caller_id] = deduped_record_paths

        if not self.post_call_command:
            self._pending_post_calls.pop(caller_id, None)
            self._pending_archive_records.pop(caller_id, None)
            return

        unit_name = self._build_post_call_unit_name(caller_id, deduped_record_paths)
        if unit_name:
            self._pending_post_calls[caller_id] = unit_name

        if self.post_call_delay_seconds > 0:
            logger.info(
                "Post-call delay of %ds for %s is delegated to the external command "
                "via GPTME_VOICE_POST_CALL_DELAY_SECONDS; the server no longer enforces it directly",
                self.post_call_delay_seconds,
                caller_id,
            )

        # Cap the dispatch command at 30s so a hung systemd-run can't stall _on_call_end
        # indefinitely. The dispatch command (e.g. post-call-dispatch.sh) is expected to exit
        # in <1s after scheduling a systemd timer, not after the full post-call delay.
        try:
            await asyncio.wait_for(
                self._run_post_call_command(
                    caller_id,
                    deduped_record_paths,
                    delay_seconds=self.post_call_delay_seconds,
                    unit_name=unit_name,
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Post-call dispatch command timed out after 30s for %s — "
                "follow-up may not have been scheduled",
                caller_id,
            )

    def _make_handoff_callback(
        self,
        caller_id_ref: list[str | None],
        transcript_ref: list[TranscriptTurn],
    ):
        """Return an async callback for tool_bridge.on_handoff that captures call context.

        ``caller_id_ref[0]`` and ``transcript_ref`` are mutable containers so the
        callback always sees the current transcript at the moment of the handoff, not
        the snapshot from when the callback was created.
        """

        async def _on_handoff(
            to_agent: str, reason: str, context_summary: str | None
        ) -> dict:
            if self._handoff_writer is None:
                return {
                    "status": "not_supported",
                    "message": (
                        "Handoff is not configured. "
                        "Set GPTME_VOICE_HANDOFF_DIR to enable cross-agent transfers."
                    ),
                }
            caller_id = caller_id_ref[0]
            if not caller_id:
                return {
                    "status": "error",
                    "message": "Cannot initiate handoff: caller identity not yet established.",
                }
            transcript_dicts = [
                {"role": t.role, "text": t.text} for t in transcript_ref
            ]
            extra: dict = {}
            if context_summary:
                extra["context_summary"] = context_summary
            try:
                published = self._handoff_writer.initiate(
                    to_agent=to_agent,
                    caller_id=caller_id,
                    reason=reason,
                    transcript=transcript_dicts,
                    extra=extra or None,
                )
                logger.info(
                    "Handoff published: id=%s to=%s path=%s",
                    published.payload["handoff_id"],
                    to_agent,
                    published.path,
                )
                return {
                    "status": "handoff_initiated",
                    "handoff_id": published.payload["handoff_id"],
                    "to_agent": to_agent,
                    "message": (
                        f"Transfer to {to_agent} initiated. "
                        "The caller will be connected shortly."
                    ),
                }
            except (ValueError, OSError) as exc:
                logger.warning("Handoff failed: %s", exc)
                return {"status": "error", "message": str(exc)}

        return _on_handoff

    def _promote_transcript_to_gptme(
        self,
        caller_id: str,
        transcript: list[TranscriptTurn],
        metadata: dict[str, str],
    ) -> None:
        """POST the call transcript to the gptme server conversation endpoint.

        Called as a fire-and-forget background task after the archive record
        is written.  Idempotent: the gptme server deduplicates by *call_sid*,
        so retries are safe.

        Requires ``GPTME_VOICE_GPTME_SERVER_URL`` and
        ``GPTME_VOICE_GPTME_SERVER_KEY`` env vars.  Silently skips when
        either is empty or missing.
        """
        if not self.gptme_server_url or not self.gptme_server_key:
            return

        call_sid = metadata.get("call_sid")
        if not call_sid:
            logger.warning(
                "Skipping transcript promotion for %s: no call_sid in metadata",
                caller_id,
            )
            return

        turns = [
            {"role": turn.role, "text": turn.text}
            for turn in transcript
            if turn.text.strip()
        ]
        if not turns:
            logger.info(
                "Skipping transcript promotion for %s: empty transcript", caller_id
            )
            return

        url = f"{self.gptme_server_url.rstrip('/')}/api/v2/conversations/{urllib.parse.quote(caller_id, safe='')}/transcript"
        payload = json.dumps(
            {
                "turns": turns,
                "call_metadata": {"call_sid": call_sid},
            }
        ).encode("utf-8")

        try:
            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(
                None,
                _http_post_sync,
                url,
                payload,
                self.gptme_server_key,
            )
            fut.add_done_callback(lambda _: None)
        except RuntimeError:
            # No running loop (tests / sync context) — post synchronously
            _http_post_sync(url, payload, self.gptme_server_key)

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
        record_path = self._save_call_record(record)
        pending_record_paths = list(self._pending_archive_records.get(caller_id, []))
        pending_record_paths.append(record_path)
        deduped_record_paths = self._dedupe_record_paths(pending_record_paths)
        record.archive_record_paths = [str(path) for path in deduped_record_paths]
        record.pending_post_call_unit = self._build_post_call_unit_name(
            caller_id, deduped_record_paths
        )
        self._save_recent_call(record)
        await self._schedule_post_call(caller_id, deduped_record_paths)

        # Promote transcript to gptme server for persistence in conversation log.
        # Fire-and-forget — never blocks the call-end teardown.
        self._promote_transcript_to_gptme(caller_id, transcript, metadata)

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

    async def serve_browser_client(self, request: Request) -> FileResponse:
        """Serve the browser WebSocket client HTML."""
        static_dir = Path(__file__).parent.parent / "static"
        return FileResponse(static_dir / "index.html", media_type="text/html")

    async def handle_incoming_call(self, request: Request) -> PlainTextResponse:
        """
        Handle incoming Twilio call — return TwiML to connect to Media Stream.

        Configure your Twilio phone number's Voice webhook to POST to this endpoint.
        Twilio will then open a Media Stream WebSocket to /twilio.
        """
        form_params = dict(await request.form())
        from_number = (form_params.get("From", "") or "").strip()
        incoming_call_sid = (
            form_params.get("CallSid", "") or form_params.get("call_sid", "") or ""
        ).strip()

        # Validate Twilio webhook signature when auth token is configured.
        # Skip in dev environments where TWILIO_AUTH_TOKEN is absent.
        # Body-tool grants are fail-closed: they are only minted when this
        # request was signature-validated. An unsigned /incoming must never
        # mint a grant from a spoofable From field.
        signature_validated = False
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
            signature_validated = True

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

        # Fire-and-forget: pre-warm the provider connection so it is ready before
        # Twilio's media-stream WebSocket sends its "start" event.  This eliminates
        # most of the ~1-3s dead air between call answer and first greeting audio.
        if from_number:
            self._register_prewarm_task(from_number)

        # Forward caller number to WebSocket handler via TwiML custom parameters.
        custom_params: dict[str, str] = {}
        if from_number:
            custom_params["from_number"] = from_number
            # Body tools on /twilio must not trust client-supplied from_number.
            # Mint a call-scoped grant here only after signature validation, and
            # bind it to CallSid (kept off TwiML so a stolen grant cannot be
            # replayed onto a different start event).
            if (
                signature_validated
                and incoming_call_sid
                and self._twilio_body_caller_allowed(from_number)
            ):
                grant_token = self._mint_twilio_body_grant(
                    from_number, incoming_call_sid
                )
                if grant_token:
                    custom_params["body_grant"] = grant_token
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
        g711_passthrough = self.openai_g711_passthrough

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
                    standup_brief = custom_params.get("standup_brief") or None
                    caller_id = from_number or call_sid or stream_sid
                    metadata = {
                        "from_number": from_number,
                        "call_sid": call_sid,
                        "stream_sid": stream_sid,
                        "provider": self.provider,
                    }
                    if handoff_id:
                        metadata["handoff_id"] = handoff_id

                    # Callbacks — closures capture stream_sid and transcript from this scope.
                    # Use a factory to bind stream_sid by value so the async coroutine
                    # is detected and awaited by _call_callback.
                    def _make_on_audio(_stream_sid: str):
                        async def _on_audio(audio: bytes) -> None:
                            payload = (
                                audio
                                if g711_passthrough
                                else audio_converter.openai_to_twilio(audio)
                            )
                            await self._send_to_twilio(websocket, _stream_sid, payload)

                        return _on_audio

                    def _make_on_speech_started(_stream_sid: str):
                        async def _on_speech_started() -> None:
                            logger.debug(
                                "Caller speech detected; clearing Twilio playback buffer for %s",
                                _stream_sid,
                            )
                            await self._send_twilio_clear(websocket, _stream_sid)

                        return _on_speech_started

                    on_audio = _make_on_audio(stream_sid)
                    on_speech_started = _make_on_speech_started(stream_sid)
                    on_ai_transcript, on_user_transcript, _twilio_hangup = (
                        self._make_transcript_callbacks(
                            transcript=transcript,
                            websocket=websocket,
                            source="twilio",
                            call_sid=call_sid,
                        )
                    )

                    # Body-tool authorization is the signed-webhook grant, not
                    # customParameters.from_number (attacker-controlled on /twilio).
                    grant = self._consume_twilio_body_grant(
                        custom_params.get("body_grant") or None
                    )
                    granted_from: str | None = None
                    if grant is not None:
                        grant_from, grant_sid = grant
                        if grant_from != from_number:
                            logger.warning(
                                "Twilio body grant From mismatch: grant=%s start=%s",
                                grant_from,
                                from_number,
                            )
                        elif not grant_sid or grant_sid != call_sid:
                            logger.warning(
                                "Twilio body grant CallSid mismatch: grant=%s start=%s",
                                grant_sid,
                                call_sid,
                            )
                        else:
                            granted_from = grant_from
                    body_adapter = self._body_adapter_for_websocket(
                        websocket,
                        transport="twilio",
                        caller_id=granted_from,
                    )

                    # Try to claim a pre-warmed session (no handoff/standup for inbound fresh calls)
                    prewarm_eligible = (
                        from_number and not handoff_id and not standup_brief
                    )
                    # A spoofed start event must not steal a body-capable prewarm.
                    if (
                        prewarm_eligible
                        and self.body_adapter is not None
                        and self._twilio_body_caller_allowed(from_number)
                        and granted_from is None
                    ):
                        prewarm_eligible = False
                    prewarm_client = (
                        await self._claim_prewarm(from_number)
                        if prewarm_eligible
                        else None
                    )

                    if prewarm_client is not None:
                        realtime_client = prewarm_client
                        realtime_client.on_audio = on_audio
                        realtime_client.on_ai_transcript = on_ai_transcript
                        realtime_client.on_user_transcript = on_user_transcript
                        realtime_client.on_speech_started = on_speech_started
                    else:
                        # Cold path: build session from scratch
                        bootstrap = await self._build_session_bootstrap(
                            caller_id=caller_id,
                            from_number=from_number,
                            handoff_id=handoff_id,
                            standup_brief=standup_brief,
                        )
                        instructions = bootstrap.instructions
                        initial_response_instructions = (
                            bootstrap.initial_response_instructions
                            if bootstrap.should_greet_first
                            else ""
                        )
                        session_cfg = self._build_session_config(
                            instructions=instructions,
                            initial_response_instructions=initial_response_instructions,
                            include_body_tools=body_adapter is not None,
                        )
                        realtime_client = self._make_client(
                            session_cfg,
                            on_audio=on_audio,
                            on_ai_transcript=on_ai_transcript,
                            on_user_transcript=on_user_transcript,
                            on_speech_started=on_speech_started,
                        )

                    # Wire tool bridge BEFORE connect/activate so on_function_call
                    # is set when the initial greeting response fires.
                    def _make_twilio_cue_callback(_ws, _sid: str, _mulaw: bytes):
                        async def _cb() -> None:
                            await self._send_to_twilio(_ws, _sid, _mulaw)

                        return _cb

                    tool_bridge = GptmeToolBridge(
                        workspace=self.workspace,
                        on_result=realtime_client.inject_message,
                        on_dispatch=_make_twilio_cue_callback(
                            websocket, stream_sid, DISPATCH_CUE_MULAW
                        ),
                        on_timeout=_make_twilio_cue_callback(
                            websocket, stream_sid, TIMEOUT_CUE_MULAW
                        ),
                        on_hangup=_twilio_hangup,
                        on_handoff=self._make_handoff_callback([caller_id], transcript),
                        transcript_provider=lambda: transcript,
                        body_adapter=body_adapter,
                    )
                    realtime_client.on_function_call = tool_bridge.handle_function_call

                    if prewarm_client is not None:
                        await realtime_client.activate_session()
                    else:
                        await realtime_client.connect()

                    self._connections[call_sid] = (websocket, realtime_client)
                    # A reconnect arrived inside the idle window — keep the grant.
                    self._cancel_twilio_body_grant_idle_revoke(call_sid)

                elif event == "media":
                    # Audio chunk from Twilio
                    if realtime_client:
                        # Extract μ-law audio
                        media = data.get("media", {})
                        mulaw_b64 = media.get("payload", "")
                        if mulaw_b64:
                            mulaw_data = base64.b64decode(mulaw_b64)
                            if g711_passthrough:
                                # OpenAI session is configured for g711_ulaw —
                                # forward Twilio's μ-law payload as-is.
                                await realtime_client.send_audio(mulaw_data)
                            else:
                                # Convert to PCM 24kHz and send to realtime API
                                pcm_data = audio_converter.twilio_to_openai(mulaw_data)
                                await realtime_client.send_audio(pcm_data)

                elif event == "stop":
                    # Real call end — revoke so a reconnect after hangup cannot
                    # re-attach motion tools with the stolen start parameters.
                    # Do not revoke in ``finally``: websocket drop is a reconnect.
                    # Idle-revoke in ``finally`` covers the drop-without-stop path.
                    self._revoke_twilio_body_grants_for_call(call_sid)
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
            # Websocket drop ≠ call end. If this CallSid does not reconnect
            # inside the idle window, drop the pinned grant so an abrupt
            # hangup that skipped ``stop`` cannot leave a live bearer token.
            self._schedule_twilio_body_grant_idle_revoke(call_sid)
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

    async def _send_twilio_clear(self, websocket, stream_sid: str) -> None:
        """Flush any queued assistant audio from Twilio's playback buffer."""

        await websocket.send_text(
            json.dumps({"event": "clear", "streamSid": stream_sid})
        )

    def _build_session_config(
        self,
        instructions: str,
        initial_response_instructions: str = "",
        *,
        include_body_tools: bool = True,
        include_vision_tools: bool = False,
    ) -> SessionConfig:
        """Build a SessionConfig with optional runtime overrides."""
        kwargs: dict = dict(
            instructions=instructions,
            initial_response_instructions=initial_response_instructions,
            available_agents=self._available_agents,
        )
        if self.model:
            kwargs["model"] = self.model
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self.voice:
            kwargs["voice"] = self.voice
        if self.output_speed is not None:
            kwargs["output_speed"] = self.output_speed
        if self.openai_g711_passthrough:
            kwargs["g711_passthrough"] = True
        extra_tools: list[dict] = []
        if include_body_tools and self.body_adapter is not None:
            extra_tools.extend(body_tool_schemas(self.body_adapter))
        if include_vision_tools:
            extra_tools.append(vision_tool_schema())
        if extra_tools:
            kwargs["extra_tools"] = extra_tools
        return SessionConfig(**kwargs)

    def _make_client(
        self,
        session_config: SessionConfig,
        hold_initial_response: bool = False,
        **kwargs,
    ) -> OpenAIRealtimeClient:
        """Instantiate the realtime client for the configured provider."""
        if self.provider == _PROVIDER_GROK:
            return XAIRealtimeClient(
                api_key=self._api_key,
                session_config=session_config,
                hold_initial_response=hold_initial_response,
                **kwargs,
            )
        return OpenAIRealtimeClient(
            api_key=self._api_key,
            session_config=session_config,
            hold_initial_response=hold_initial_response,
            **kwargs,
        )

    def _make_transcript_callbacks(
        self,
        *,
        transcript: list[TranscriptTurn],
        websocket,
        source: str,
        call_sid: str | None,
    ):
        hangup_task: asyncio.Task[None] | None = None

        def _request_hangup(trigger: str, reason: str | None) -> None:
            nonlocal hangup_task
            if hangup_task is not None and not hangup_task.done():
                logger.info(
                    "Ignoring duplicate hangup request: source=%s trigger=%s call_sid=%s",
                    source,
                    trigger,
                    call_sid,
                )
                return
            hangup_task = asyncio.create_task(
                self._schedule_hangup(
                    websocket,
                    source=f"{source}:{trigger}",
                    reason=reason,
                    call_sid=call_sid,
                )
            )

        async def _on_hangup(reason: str | None) -> None:
            _request_hangup("tool", reason)

        async def _on_ai_transcript(text: str) -> None:
            _append_transcript_turn(
                transcript, "assistant", text, allow_continuation=False
            )
            if _should_trigger_hangup_transcript_fallback(transcript, text):
                logger.warning(
                    "Assistant committed to hanging up without tool; scheduling transcript fallback: %s",
                    text,
                )
                _request_hangup(
                    "transcript-fallback",
                    f"assistant said: {text[:120]}",
                )

        def _on_user_transcript(text: str, item_id: str | None = None) -> None:
            _append_transcript_turn(transcript, "user", text, item_id=item_id)

        return _on_ai_transcript, _on_user_transcript, _on_hangup

    @staticmethod
    def _make_sound_cue_callback(websocket, cue: str):
        async def _send_cue() -> None:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "sound_cue",
                        "cue": cue,
                        "sample_rate": SAMPLE_RATE,
                        "audio": base64.b64encode(PCM_CUES[cue]).decode("ascii"),
                    }
                )
            )

        return _send_cue

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
            instructions = await self._build_session_instructions(
                caller_id=caller_id,
                handoff_id=handoff_id,
            )
            body_adapter = self._body_adapter_for_websocket(
                websocket, transport="local"
            )
            vision_bridge = VisionSessionBridge(websocket.send_text)
            session_cfg = self._build_session_config(
                instructions=instructions,
                include_body_tools=body_adapter is not None,
                include_vision_tools=True,
            )
            on_ai_transcript, on_user_transcript, _local_hangup = (
                self._make_transcript_callbacks(
                    transcript=transcript,
                    websocket=websocket,
                    source="local",
                    call_sid=None,
                )
            )
            realtime_client = self._make_client(
                session_cfg,
                on_audio=lambda audio: self._send_local_audio(websocket, audio),
                on_audio_end=lambda: self._send_local_audio_end(websocket),
                on_ai_transcript=on_ai_transcript,
                on_user_transcript=on_user_transcript,
            )

            tool_bridge = GptmeToolBridge(
                workspace=self.workspace,
                on_result=realtime_client.inject_message,
                on_dispatch=self._make_sound_cue_callback(websocket, "dispatch"),
                on_timeout=self._make_sound_cue_callback(websocket, "timeout"),
                on_hangup=_local_hangup,
                on_handoff=self._make_handoff_callback([caller_id], transcript),
                transcript_provider=lambda: transcript,
                body_adapter=body_adapter,
                vision_bridge=vision_bridge,
            )
            realtime_client.on_function_call = tool_bridge.handle_function_call

            await realtime_client.connect()

            async for message in websocket.iter_text():
                data = json.loads(message)

                if await vision_bridge.handle_message(data):
                    continue
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
            if "vision_bridge" in locals():
                vision_bridge.close()
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

    async def handle_browser_websocket(self, websocket):
        """
        Handle WebSocket connection for browser voice transport.

        Accepts raw binary PCM16 audio frames at 16kHz from the browser side.
        Control messages remain JSON text frames.
        """
        await websocket.accept()

        caller_id = self._get_local_caller_id(websocket)
        handoff_id = self._get_local_handoff_id(websocket)
        realtime_client: OpenAIRealtimeClient | None = None
        tool_bridge: GptmeToolBridge | None = None
        transcript: list[TranscriptTurn] = []
        audio_converter = AudioConverter()

        try:
            instructions = await self._build_session_instructions(
                caller_id=caller_id,
                handoff_id=handoff_id,
            )
            body_adapter = self._body_adapter_for_websocket(
                websocket, transport="browser"
            )
            session_cfg = self._build_session_config(
                instructions=instructions,
                include_body_tools=body_adapter is not None,
            )
            on_ai_transcript, on_user_transcript, _browser_hangup = (
                self._make_transcript_callbacks(
                    transcript=transcript,
                    websocket=websocket,
                    source="browser",
                    call_sid=None,
                )
            )
            realtime_client = self._make_client(
                session_cfg,
                on_audio=lambda audio: self._send_browser_audio(websocket, audio),
                on_audio_end=lambda: self._send_browser_audio_end(websocket),
                on_ai_transcript=on_ai_transcript,
                on_user_transcript=on_user_transcript,
            )

            tool_bridge = GptmeToolBridge(
                workspace=self.workspace,
                on_result=realtime_client.inject_message,
                on_dispatch=self._make_sound_cue_callback(websocket, "dispatch"),
                on_timeout=self._make_sound_cue_callback(websocket, "timeout"),
                on_hangup=_browser_hangup,
                on_handoff=self._make_handoff_callback([caller_id], transcript),
                transcript_provider=lambda: transcript,
                body_adapter=body_adapter,
            )
            realtime_client.on_function_call = tool_bridge.handle_function_call

            await realtime_client.connect()
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "ready",
                        "input_sample_rate": AudioConverter.BROWSER_RATE,
                        "output_sample_rate": AudioConverter.OPENAI_RATE,
                    }
                )
            )

            while True:
                message = await websocket.receive()
                message_type = message.get("type")
                if message_type == "websocket.disconnect":
                    break

                audio_data = message.get("bytes")
                if isinstance(audio_data, bytes):
                    await realtime_client.send_audio(
                        audio_converter.browser_to_openai(audio_data)
                    )
                    continue

                text = message.get("text")
                if not isinstance(text, str):
                    continue

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(
                        "Browser websocket: ignoring malformed JSON text frame"
                    )
                    continue
                if data.get("type") == "commit":
                    await realtime_client.commit_audio()

        except WebSocketDisconnect:
            pass
        except RuntimeError as exc:
            if "not connected" not in str(exc).lower():
                raise
            logger.debug("Browser websocket already closed before receive: %s", exc)
        except Exception as e:
            logger.exception("Error handling browser connection: %s", e)
        finally:
            if realtime_client:
                await self._disconnect_realtime_client(realtime_client)
            await self._on_call_end(
                caller_id,
                "browser",
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

        For Twilio calls, also fires the REST API ``calls.update(status='completed')``
        as the authoritative kill — closing the WebSocket alone does NOT terminate
        the Twilio call at the platform level, which is why calls could continue
        long after the hangup tool was invoked.
        """
        logger.info(
            "Hangup scheduled: source=%s call_sid=%s reason=%s",
            source,
            call_sid,
            reason or "<none>",
        )
        # Hangup is a real call end even if Twilio never sends ``stop``.
        if "twilio" in source:
            self._revoke_twilio_body_grants_for_call(call_sid)

        # Fire-and-forget Twilio REST API call termination (authoritative kill).
        # Do this BEFORE the farewell delay so the call stops accepting audio
        # from the caller immediately — the farewell utterance was already sent
        # by the model before it called the hangup tool.
        twilio_account_sid = _get_config_env("TWILIO_ACCOUNT_SID")
        twilio_auth_token = _get_config_env("TWILIO_AUTH_TOKEN")
        if call_sid and twilio_account_sid and twilio_auth_token and "twilio" in source:
            try:
                from twilio.rest import Client as TwilioClient

                client = TwilioClient(twilio_account_sid, twilio_auth_token)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, lambda: client.calls(call_sid).update(status="completed")
                )
                logger.info("Twilio call %s terminated via REST API", call_sid)
            except Exception as exc:
                logger.warning(
                    "Failed to terminate Twilio call %s via REST API: %s",
                    call_sid,
                    exc,
                )

        # Brief delay so any buffered farewell audio still plays.
        try:
            await asyncio.sleep(_HANGUP_FAREWELL_DELAY_SECONDS)
        except asyncio.CancelledError:
            raise

        # Close the WebSocket to stop the media stream.
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

    async def _send_browser_audio(self, websocket, audio_data: bytes):
        """Send raw PCM audio frames to the browser transport."""
        await websocket.send_bytes(audio_data)

    async def _send_browser_audio_end(self, websocket):
        """Signal to the browser transport that playback is complete."""
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
@click.option(
    "--reasoning-effort",
    default="low",
    type=click.Choice(_VALID_REASONING_EFFORTS),
    show_default=True,
    help=(
        "OpenAI Realtime reasoning effort. Ignored for xAI. OpenAI recommends "
        "starting gpt-realtime-2 at low for production voice agents."
    ),
)
@click.option(
    "--voice",
    default=None,
    help=(
        "Override the provider voice. For OpenAI Realtime, current voices include "
        "echo, alloy, ash, ballad, coral, sage, shimmer, verse, marin, and cedar."
    ),
)
@click.option(
    "--output-speed",
    default=None,
    type=click.FloatRange(0.25, 1.5),
    help=(
        "Override spoken output speed. Currently passed through only for OpenAI "
        "Realtime, which supports 0.25 to 1.5."
    ),
)
@click.option(
    "--enable-browser-transport",
    is_flag=True,
    help="Expose /voice WebSocket for browser PCM transport.",
)
@click.option("--debug", is_flag=True, help="Enable debug logging")
def main(
    host: str,
    port: int,
    workspace: str | None,
    provider: str,
    model: str | None,
    reasoning_effort: str,
    voice: str | None,
    output_speed: float | None,
    enable_browser_transport: bool,
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
        reasoning_effort=reasoning_effort,
        voice=voice,
        output_speed=output_speed,
        enable_browser_transport=enable_browser_transport,
    )

    logger.info(
        "Starting voice server on %s:%s (provider=%s, model=%s, reasoning_effort=%s)",
        host,
        port,
        provider,
        model or "<default>",
        reasoning_effort if provider == _PROVIDER_OPENAI else "<ignored>",
    )
    logger.info(f"Local test endpoint: ws://{host}:{port}/local")
    if enable_browser_transport:
        logger.info(f"Browser WebSocket endpoint: ws://{host}:{port}/voice")
        logger.info(f"Browser client UI: http://{host}:{port}/browser")

    server.run()


if __name__ == "__main__":
    main()
