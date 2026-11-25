"""Prepared call purposes, offered context and explicit fulfillment receipts.

Registration never marks a plan fulfilled. Only a caller-confirmed tool action
bound to a plan offered during an authenticated call can write the receipt.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

MAX_PLAN_AGE = timedelta(hours=24)
MAX_CONTEXT_BYTES = 16000
_PLAN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("now must include timezone")
    return value.astimezone(timezone.utc)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(timezone.utc)


def _path(workspace: str, plan_id: str, *, receipt: bool = False) -> Path:
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise ValueError("invalid plan_id")
    folder = "fulfilled" if receipt else "planned"
    return Path(workspace) / "state" / "voice-calls" / folder / f"{plan_id}.json"


def _write_once(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish complete JSON exclusively. Readers never see a half-written plan,
    # and a crashed writer cannot poison the permanent occurrence ID.
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def register_planned_call(
    workspace: str,
    plan_id: str,
    caller: str,
    scheduled_at: str,
    purpose: str,
    context: dict,
) -> dict:
    """Create an immutable prepared plan; retries return its existing snapshot.

    IDs identify one scheduled purpose, not a recurring timer. A later occurrence
    needs a new ID. The context is copied inline so mutable brief paths cannot
    silently replace the purpose the caller was offered.
    """
    path = _path(workspace, plan_id)
    _timestamp(scheduled_at)
    _timestamp(context.get("generated_at"))
    if not caller.strip() or not purpose.strip():
        raise ValueError("caller and purpose are required")
    if not isinstance(context.get("text"), str) or not context["text"].strip():
        raise ValueError("context text is required")
    if len(json.dumps(context, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
        raise ValueError("context exceeds size limit")
    plan = {
        "plan_id": plan_id,
        "caller": caller,
        "scheduled_at": scheduled_at,
        "purpose": purpose,
        "context": context,
    }
    try:
        _write_once(path, plan)
    except FileExistsError:
        existing = json.loads(path.read_text())
        if not isinstance(existing, dict):
            raise ValueError("existing planned call is invalid")
        if (
            existing.get("caller") != caller
            or existing.get("scheduled_at") != scheduled_at
            or existing.get("purpose") != purpose
        ):
            raise ValueError(
                "plan_id already identifies a different scheduled purpose"
            ) from None
        return existing
    return plan


def is_planned_call_fulfilled(workspace: str, plan_id: str) -> bool:
    """Return whether a valid explicit fulfillment receipt exists for this ID."""
    try:
        receipt = json.loads(_path(workspace, plan_id, receipt=True).read_text())
        _timestamp(receipt.get("fulfilled_at"))
        return bool(
            receipt.get("plan_id") == plan_id
            and receipt.get("call_sid")
            and receipt.get("caller_confirmation")
            and receipt.get("summary")
        )
    except (OSError, ValueError, AttributeError):
        return False


def load_pending_planned_calls(
    workspace: str | None, caller: str, *, now: datetime | None = None
) -> list[dict]:
    """Load bounded, fresh plans for this caller scheduled in the next 24 hours."""
    if not workspace:
        return []
    current = _now(now)
    plans = []
    for path in sorted((Path(workspace) / "state/voice-calls/planned").glob("*.json")):
        try:
            if path.stat().st_size > MAX_CONTEXT_BYTES + 4096:
                continue
            plan = json.loads(path.read_text())
            plan_id = plan["plan_id"]
            if path != _path(workspace, plan_id) or plan["caller"] != caller:
                continue
            scheduled = _timestamp(plan["scheduled_at"])
            context = plan["context"]
            generated = _timestamp(context["generated_at"])
            if not (
                timedelta(0) <= scheduled - current <= MAX_PLAN_AGE
                and timedelta(0) <= current - generated <= MAX_PLAN_AGE
                and isinstance(context.get("text"), str)
                and context["text"].strip()
                and isinstance(plan.get("purpose"), str)
                and plan["purpose"].strip()
            ):
                continue
            if not is_planned_call_fulfilled(workspace, plan_id):
                plans.append(plan)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # A faulty producer must not inflate every voice session without bound.
    return sorted(plans, key=lambda plan: _timestamp(plan["scheduled_at"]))[:3]


def planned_call_guidance(plans: list[dict]) -> str:
    return (
        "PREPARED UPCOMING CALLS:\n"
        "The caller called before these planned calls. Their prepared context is below. "
        "Answer brief-covered questions directly from it, at a high level focused on "
        "outcomes and decisions. Do not recite detailed in-progress PR status. "
        "Offer the relevant brief naturally and follow the caller's lead. "
        "These calls remain scheduled unless their purpose is actually completed. "
        "Only after covering a plan's whole purpose, ask whether that covers the "
        "planned purpose and whether the later call is unnecessary. Name that plan's "
        "purpose explicitly in the question, so a natural yes is unambiguous. If the caller "
        "explicitly confirms coverage (for example, 'that covers the daily standup'), "
        "call fulfill_planned_call with their exact words and the covered summary. "
        "An unrelated, partial, or interrupted discussion does not fulfill a plan. "
        "A yes qualifies only after an explicit question confirming both full purpose coverage and skipping the later call. Never report cancellation before the tool succeeds.\n\n"
        + json.dumps(plans, ensure_ascii=False)
    )


def fulfillment_tool_schema() -> dict:
    return {
        "type": "function",
        "name": "fulfill_planned_call",
        "description": (
            "Record that an offered planned call's full purpose was covered during this "
            "call and the caller explicitly confirmed coverage and that the later "
            "call is unnecessary. Quote their exact confirmation. A yes qualifies only "
            "after an explicit combined purpose-coverage and skip-call question; "
            "a greeting or partial discussion never qualifies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "caller_confirmation": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["plan_id", "caller_confirmation", "summary"],
            "additionalProperties": False,
        },
    }


def fulfill_planned_call(
    workspace: str,
    *,
    offered_plans: list[dict],
    caller: str,
    call_sid: str,
    transcript: Sequence[object],
    plan_id: str,
    caller_confirmation: str,
    summary: str,
    now: datetime | None = None,
) -> dict:
    """Record explicit coverage only for a purpose offered on this call.

    Transcript binding rejects invented confirmation quotes. Purpose assessment
    remains the agent's responsibility; the receipt retains the evidence for audit.
    """
    plan = next((p for p in offered_plans if p.get("plan_id") == plan_id), None)
    if not plan or plan.get("caller") != caller or not call_sid:
        return {
            "error": "This planned call was not offered to this authenticated caller."
        }
    if not all(
        isinstance(value, str) and value.strip()
        for value in (caller_confirmation, summary)
    ):
        return {
            "error": "Exact caller confirmation and a covered-purpose summary are required."
        }
    confirmation = " ".join(caller_confirmation.lower().split())
    purpose_words = re.findall(r"[a-z0-9]+", plan["purpose"].lower())
    # A superseded positive quote cannot override a later retraction.
    latest_user = next(
        (
            index
            for index in range(len(transcript) - 1, -1, -1)
            if getattr(transcript[index], "role", None) == "user"
        ),
        None,
    )
    if (
        latest_user is None
        or " ".join(getattr(transcript[latest_user], "text", "").lower().split())
        != confirmation
    ):
        return {"error": "The exact confirmation must be the latest caller turn."}
    if not any(
        getattr(turn, "role", None) == "assistant" and getattr(turn, "text", "").strip()
        for turn in transcript[:latest_user]
    ):
        return {"error": "The planned purpose must be discussed before confirmation."}
    preceding_turn = transcript[latest_user - 1] if latest_user > 0 else None
    preceding_assistant = (
        getattr(preceding_turn, "text", "").lower()
        if getattr(preceding_turn, "role", None) == "assistant"
        else ""
    )
    # Coverage alone does not authorize suppressing a later call. The quote
    # either says it is unnecessary or answers an explicit combined question.
    explicit_cancellation = bool(
        re.search(
            r"\b(?:no need (?:for )?(?:the )?(?:later|scheduled|planned) call|"
            r"(?:the )?(?:later|scheduled|planned) call is unnecessary)\b",
            confirmation,
        )
    )
    combined_question = (
        "?" in preceding_assistant
        and bool(
            re.search(r"\b(?:cover|complete|fulfill)(?:s|d)?\b", preceding_assistant)
        )
        and not re.search(r"\b(?:not|never|keep)\b|don.t", preceding_assistant)
        and all(
            word in re.findall(r"[a-z0-9]+", preceding_assistant)
            for word in purpose_words
        )
        and bool(
            re.search(r"\b(?:later|scheduled|planned) call\b", preceding_assistant)
        )
        and bool(
            re.search(
                r"\b(?:skip|cancel|unnecessary|no longer needed)\b", preceding_assistant
            )
        )
    )
    affirmative = bool(
        re.fullmatch(
            r"(?:yes|yeah|yep|correct|exactly|that['’]s right)[.! ]*", confirmation
        )
    )
    explicit_coverage = (
        bool(
            re.fullmatch(
                r"(?:yes[,!. ]+)?(?:that|this) (?:covers|completes|fulfills) [a-z0-9 ,.’'-]+[.!]?",
                confirmation,
            )
        )
        and all(
            word in re.findall(r"[a-z0-9]+", confirmation) for word in purpose_words
        )
        and not re.search(
            r"\b(?:not|no|never|partly|partially|except|but|only|still|keep|incomplete)\b",
            confirmation.replace("no need", ""),
        )
    )
    if not (
        (combined_question and (affirmative or explicit_coverage))
        or (explicit_coverage and explicit_cancellation)
    ):
        return {
            "error": "Caller must also confirm the later planned call is unnecessary."
        }
    receipt = {
        "plan_id": plan_id,
        "caller": caller,
        "call_sid": call_sid,
        "fulfilled_at": _now(now).isoformat(),
        "caller_confirmation": caller_confirmation,
        "summary": summary,
        "purpose": plan["purpose"],
    }
    try:
        _write_once(_path(workspace, plan_id, receipt=True), receipt)
    except FileExistsError:
        if not is_planned_call_fulfilled(workspace, plan_id):
            return {
                "error": "Existing fulfillment receipt is invalid; planned call remains scheduled."
            }
    except OSError as exc:
        return {
            "error": f"Could not record fulfillment; planned call remains scheduled: {exc}"
        }
    return {
        "status": "fulfilled",
        "plan_id": plan_id,
        "message": "This planned call is fulfilled; its scheduler may suppress the redundant call.",
    }
