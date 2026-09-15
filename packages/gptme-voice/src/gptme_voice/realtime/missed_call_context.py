"""General missed-call context persistence for inbound callback sessions.

When an outbound call goes unanswered, the caller writes a lightweight context
note (``write_missed_call_context``). When the same party calls back within
``CALLBACK_WINDOW``, the inbound path loads that note and injects its content
into the session — as if the call took place but the other end was silent.

The standup case is an instance of this general pattern. The outbound standup
path writes a note with ``type="standup"`` and the prepared brief as context;
the inbound path loads it via the same generic reader, unaware of the type.

Legacy backward compat: if no ``missed-call-context.json`` exists, the loader
falls back to reading ``last-standup-call-sid.txt`` + ``standup-brief.json``
so existing deployments continue to work without changes.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)
CALLBACK_WINDOW = timedelta(minutes=30)
MAX_CONTEXT_AGE = timedelta(hours=4)
_MAX_PAYLOAD_BYTES = 16000
_CONTEXT_NOTE_FILE = "missed-call-context.json"
_LEGACY_STAMP_FILE = "last-standup-call-sid.txt"
_LEGACY_BRIEF_FILE = "standup-brief.json"

CALLBACK_GUIDANCE = (
    "MISSED CALL CALLBACK:\n"
    "You attempted to reach this caller earlier; the call went unanswered. "
    "The prepared context from that call is below, dated to when it was generated. "
    "Answer questions covered by this context directly without searching or "
    "dispatching a subagent. Treat facts in the context as of their generation time. "
    "The caller may have another reason for calling; follow their lead."
)
CALLBACK_GREETING = (
    "This is an inbound callback. Greet the caller briefly and mention you tried "
    "to reach them earlier and have the prepared context ready. Ask whether they "
    "want to pick up from where you left off, then wait. Do not deliver the content "
    "until asked."
)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def write_missed_call_context(
    workspace: str | None,
    *,
    type: str = "general",
    sid: str,
    caller: str,
    context: dict,
    context_file: str | None = None,
    now: datetime | None = None,
) -> None:
    """Write a missed-call context note after an unanswered outbound call.

    Called by the outbound call path when the call ends unanswered. The note
    records the SID, caller, and prepared context so an inbound callback can
    resume without re-generating it.

    ``context`` must include a ``text`` field with the prepared summary and a
    timezone-aware ``generated_at`` timestamp. The inbound loader rejects notes
    without ``generated_at`` rather than skipping freshness checks. Additional
    structured fields (goals, bullets, etc.) are included as-is.

    ``context_file`` is optional; it names the workspace-relative path that
    was used to build the context, for audit purposes.
    """
    if not workspace:
        return
    current = now or datetime.now(timezone.utc)
    voice_dir = Path(workspace) / "state" / "voice-calls"
    voice_dir.mkdir(parents=True, exist_ok=True)
    note: dict = {
        "type": type,
        "sid": sid,
        "date": current.date().isoformat(),
        "placed_at": current.isoformat(),
        "caller": caller,
        "context": context,
    }
    if context_file is not None:
        note["context_file"] = context_file
    (voice_dir / _CONTEXT_NOTE_FILE).write_text(
        json.dumps(note, ensure_ascii=False), encoding="utf-8"
    )


def load_callback_candidate(
    workspace: str | None, *, now: datetime | None = None
) -> tuple[str, str, float] | None:
    """Return (sid, payload_json, placed_at_timestamp) for a fresh local candidate.

    Tries the generic ``missed-call-context.json`` first, then falls back to the
    legacy standup-specific files for backward compatibility.
    """
    if not workspace:
        return None
    current = now or datetime.now(timezone.utc)
    state = Path(workspace) / "state"
    result = _load_from_note(state / "voice-calls" / _CONTEXT_NOTE_FILE, current)
    if result is not None:
        return result
    return _load_legacy(state, current)


def _load_from_note(
    note_path: Path, current: datetime
) -> tuple[str, str, float] | None:
    try:
        note = json.loads(note_path.read_text())
        if not isinstance(note, dict):
            return None
        sid = note.get("sid")
        if not isinstance(sid, str) or not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid):
            return None
        placed = _timestamp(note.get("placed_at"))
        if not (
            note.get("date") == current.date().isoformat()
            and placed.date() == current.date()
            and timedelta(0) <= current - placed <= CALLBACK_WINDOW
        ):
            return None
        context = note.get("context")
        if not isinstance(context, dict):
            return None
        generated = _timestamp(context.get("generated_at"))
        if not (
            generated.date() == current.date()
            and timedelta(0) <= current - generated <= MAX_CONTEXT_AGE
            and generated <= placed
        ):
            return None
        if not isinstance(context.get("text"), str) or not context["text"].strip():
            return None
        payload = json.dumps(context, ensure_ascii=False)
        if len(payload.encode()) > _MAX_PAYLOAD_BYTES:
            return None
    except (OSError, ValueError):
        return None
    return sid, payload, placed.timestamp()


def _load_legacy(state: Path, current: datetime) -> tuple[str, str, float] | None:
    """Fall back to the old standup-specific files."""
    try:
        stamp = json.loads((state / "voice-calls" / _LEGACY_STAMP_FILE).read_text())
        brief = json.loads((state / _LEGACY_BRIEF_FILE).read_text())
        if not isinstance(stamp, dict) or not isinstance(brief, dict):
            return None
        sid = stamp.get("sid")
        if not isinstance(sid, str) or not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid):
            return None
        placed = _timestamp(stamp.get("placed_at"))
        generated = _timestamp(brief.get("generated_at"))
        if not (
            stamp.get("date") == current.date().isoformat()
            and placed.date() == generated.date() == current.date()
            and timedelta(0) <= current - placed <= CALLBACK_WINDOW
            and timedelta(0) <= current - generated <= MAX_CONTEXT_AGE
            and generated <= placed
        ):
            return None
        if not isinstance(brief.get("text"), str) or not brief["text"].strip():
            return None
        plan = {
            key: brief[key]
            for key in (
                "generated_at",
                "text",
                "opener",
                "conversation_goals",
                "discussion_points",
                "likely_followups",
                "grounded_answer_notes",
                "bullets",
            )
            if key in brief
        }
        payload = json.dumps(plan, ensure_ascii=False)
        if len(payload.encode()) > _MAX_PAYLOAD_BYTES:
            return None
    except (OSError, ValueError):
        return None
    return sid, payload, placed.timestamp()


async def load_callback_brief(
    workspace: str | None,
    caller: str,
    *,
    account_sid: str | None,
    auth_token: str | None,
    last_call_ended_at: float | None = None,
    now: datetime | None = None,
) -> str | None:
    """Caller must already be authenticated and authorized as the operator.

    Local artifacts nominate a call; Twilio confirms its recipient and terminal
    outcome. An intervening conversation takes precedence over the missed call.
    The API lookup is bounded and only runs for a fresh local candidate.
    """
    if not account_sid or not auth_token:
        return None
    if not re.fullmatch(r"AC[0-9a-fA-F]{32}", account_sid):
        return None
    candidate = load_callback_candidate(workspace, now=now)
    if candidate is None:
        return None
    sid, payload, placed_at = candidate
    if last_call_ended_at is not None and last_call_ended_at >= placed_at:
        return None

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await asyncio.wait_for(
                client.get(
                    f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{sid}.json",
                    auth=(account_sid, auth_token),
                ),
                timeout=2.0,
            )
            response.raise_for_status()
            call = response.json()
        if not isinstance(call, dict) or not (
            call.get("sid") == sid
            and call.get("to") == caller
            and call.get("direction") == "outbound-api"
            and call.get("status") in ("no-answer", "busy", "failed", "canceled")
        ):
            return None
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
        logger.info("Callback status unavailable (%s)", type(exc).__name__)
        return None
    return payload
