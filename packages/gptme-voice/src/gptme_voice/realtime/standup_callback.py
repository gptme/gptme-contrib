"""Recover a prepared standup only for a verified callback to a missed call."""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)
CALLBACK_WINDOW = timedelta(minutes=30)
MAX_BRIEF_AGE = timedelta(hours=4)
CALLBACK_GUIDANCE = (
    "MISSED STANDUP CALLBACK:\n"
    "This is an inbound callback after your scheduled standup went unanswered. "
    "The original prepared plan is below, including its generated_at timestamp. "
    "If asked 'give me the standup' or 'what did you call about?', deliver that plan "
    "directly. Answer covered follow-ups from it without searching journals or "
    "dispatching a subagent. Treat operational facts as of generated_at, not as "
    "newly verified. The caller may have another reason for calling; follow their lead."
)
CALLBACK_GREETING = (
    "This is an inbound callback. Greet the caller briefly by name and say you "
    "have the standup you called about ready. Ask whether they want it, then wait. "
    "Do not deliver the full brief until asked."
)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def load_callback_candidate(
    workspace: str | None, *, now: datetime | None = None
) -> tuple[str, str, float] | None:
    """Read fresh local evidence: outbound SID, prepared plan, placement time."""
    if not workspace:
        return None
    current = now or datetime.now(timezone.utc)
    state = Path(workspace) / "state"
    try:
        stamp = json.loads(
            (state / "voice-calls/last-standup-call-sid.txt").read_text()
        )
        brief = json.loads((state / "standup-brief.json").read_text())
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
            and timedelta(0) <= current - generated <= MAX_BRIEF_AGE
            and generated <= placed
        ):
            return None
        if not isinstance(brief.get("text"), str) or not brief["text"].strip():
            return None
        # Keep the prepared plan, not potentially huge planner provenance.
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
        if len(payload) > 16000:
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
        logger.info("Standup callback status unavailable (%s)", type(exc).__name__)
        return None
    return payload
