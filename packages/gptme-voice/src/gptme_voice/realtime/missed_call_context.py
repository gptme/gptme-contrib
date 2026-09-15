"""General missed-call context persistence for inbound callback sessions.

When an outbound call is placed, the caller writes a lightweight context note
(``write_missed_call_context``) with a path to the prepared context file. When
the same party calls back within ``CALLBACK_WINDOW``, the inbound path reads
that file and injects its content into the session — as if the call took place
but the other end was silent.

The standup case is an instance of this general pattern. The outbound standup
path writes a note with ``type="standup"`` and ``context_file`` pointing at
``state/standup-brief.json``; the inbound path loads the referenced file via
the same generic reader, unaware of the type.

An optional inlined ``context`` snapshot is a fallback for notes that already
carry one, or when the referenced file is missing or stale.

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


def _as_utc(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _resolve_workspace_file(workspace: Path, relative: str) -> Path | None:
    """Return an existing file inside *workspace*, or None if the path escapes."""
    stored = _workspace_relative_path(workspace, relative)
    if stored is None:
        return None
    candidate = (workspace.resolve() / stored).resolve()
    if not candidate.is_file():
        return None
    return candidate


def _workspace_relative_path(workspace: Path, context_file: str) -> str | None:
    """Normalize *context_file* to a posix path inside *workspace*.

    Rejects absolute paths and ``..`` traversal that would land outside the
    workspace. The file does not have to exist yet (write path).
    """
    if not context_file or not context_file.strip():
        return None
    raw = Path(context_file.strip())
    root = workspace.resolve()
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    return relative.as_posix()


def write_missed_call_context(
    workspace: str | None,
    *,
    type: str = "general",
    sid: str,
    caller: str,
    context: dict | None = None,
    context_file: str | None = None,
    now: datetime | None = None,
) -> None:
    """Write a missed-call context note when an outbound call is placed.

    The inbound loader still requires Twilio to confirm the outbound leg ended
    unanswered. The note records the SID, caller, and a workspace-relative
    ``context_file`` so the callback session can read the prepared context
    from disk — the same file the original call would have used.

    ``context`` is an optional snapshot of that file (``text`` plus a
    timezone-aware ``generated_at``). The inbound loader prefers the live
    file and falls back to this snapshot if the file is missing or stale.

    Additional structured fields (goals, bullets, etc.) in a snapshot are
    included as-is.
    """
    if not workspace:
        return
    if context is None and not context_file:
        return
    current = _as_utc(now)
    root = Path(workspace)
    voice_dir = root / "state" / "voice-calls"
    voice_dir.mkdir(parents=True, exist_ok=True)
    note: dict = {
        "type": type,
        "sid": sid,
        "date": current.date().isoformat(),
        "placed_at": current.isoformat(),
        "caller": caller,
    }
    if context_file is not None:
        relative = _workspace_relative_path(root, context_file)
        if relative is not None:
            note["context_file"] = relative
    if context is not None:
        note["context"] = context
    if "context_file" not in note and "context" not in note:
        return
    (voice_dir / _CONTEXT_NOTE_FILE).write_text(
        json.dumps(note, ensure_ascii=False), encoding="utf-8"
    )


def load_callback_candidate(
    workspace: str | None, *, now: datetime | None = None, caller: str | None = None
) -> tuple[str, str, float] | None:
    """Return (sid, payload_json, placed_at_timestamp) for a fresh local candidate.

    Tries the generic ``missed-call-context.json`` first, then falls back to the
    legacy standup-specific files for backward compatibility.

    When ``caller`` is provided, a new-format note must name that same caller
    or it is ignored (avoids a Twilio lookup for a different number). Legacy
    stamps have no caller field; Twilio remains the authority there.
    """
    if not workspace:
        return None
    current = _as_utc(now)
    root = Path(workspace)
    state = root / "state"
    result = _load_from_note(
        state / "voice-calls" / _CONTEXT_NOTE_FILE,
        current,
        workspace=root,
        caller=caller,
    )
    if result is not None:
        return result
    return _load_legacy(state, current)


def _serialize_context(
    context: object, current: datetime, placed: datetime
) -> str | None:
    if not isinstance(context, dict):
        return None
    try:
        generated = _timestamp(context.get("generated_at"))
    except ValueError:
        return None
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
    return payload


def _load_context_payload(
    note: dict,
    *,
    workspace: Path,
    current: datetime,
    placed: datetime,
) -> str | None:
    context_file = note.get("context_file")
    if isinstance(context_file, str):
        path = _resolve_workspace_file(workspace, context_file)
        if path is not None:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None
            payload = _serialize_context(loaded, current, placed)
            if payload is not None:
                return payload
    return _serialize_context(note.get("context"), current, placed)


def _load_from_note(
    note_path: Path,
    current: datetime,
    *,
    workspace: Path,
    caller: str | None = None,
) -> tuple[str, str, float] | None:
    try:
        note = json.loads(note_path.read_text())
        if not isinstance(note, dict):
            return None
        sid = note.get("sid")
        if not isinstance(sid, str) or not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid):
            return None
        if caller is not None and note.get("caller") != caller:
            return None
        placed = _timestamp(note.get("placed_at"))
        if not (
            note.get("date") == current.date().isoformat()
            and placed.date() == current.date()
            and timedelta(0) <= current - placed <= CALLBACK_WINDOW
        ):
            return None
        payload = _load_context_payload(
            note, workspace=workspace, current=current, placed=placed
        )
        if payload is None:
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
    candidate = load_callback_candidate(workspace, now=now, caller=caller)
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
