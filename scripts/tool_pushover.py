import hashlib
import os
import time
from pathlib import Path

import requests
from gptme.message import Message
from gptme.tools import Parameter, ToolSpec, ToolUse

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN")

PUSHOVER_DEDUP_TTL = 30 * 60  # 30 minutes
PUSHOVER_DEDUP_DIR = Path.home() / ".local" / "state" / "gptme" / "pushover-dedup"


def has_pushover_conf():
    return PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN


def _dedup_key(title: str, message: str, user_key: str = "") -> str:
    # Include user_key so different recipients are never cross-suppressed.
    return hashlib.sha256(f"{title}|{message}|{user_key}".encode()).hexdigest()[:16]


def _is_recent_duplicate(title: str, message: str, user_key: str = "") -> bool:
    marker = PUSHOVER_DEDUP_DIR / f"{_dedup_key(title, message, user_key)}.txt"
    try:
        return time.time() - marker.stat().st_mtime < PUSHOVER_DEDUP_TTL
    except FileNotFoundError:
        return False


def _try_claim_send(title: str, message: str, user_key: str = "") -> bool:
    """Atomically claim the send slot BEFORE sending.

    Returns True if this caller owns the slot and should proceed with the send.
    On True + failed send, call _release_claim() so the next attempt is not blocked.

    Uses O_CREAT|O_EXCL: only one concurrent caller can create the marker;
    all others see FileExistsError and return False, preventing duplicate sends.
    """
    PUSHOVER_DEDUP_DIR.mkdir(parents=True, exist_ok=True)
    marker = PUSHOVER_DEDUP_DIR / f"{_dedup_key(title, message, user_key)}.txt"

    # Clear any expired marker so the exclusive create can succeed.
    try:
        if time.time() - marker.stat().st_mtime < PUSHOVER_DEDUP_TTL:
            return False  # recent duplicate
        marker.unlink()  # expired — make way for new claim
    except FileNotFoundError:
        pass

    # Atomic claim: only one caller succeeds.
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        return False  # concurrent session beat us


def _release_claim(title: str, message: str, user_key: str = "") -> None:
    """Release a pre-send claim on failure so the next attempt is not deduped."""
    marker = PUSHOVER_DEDUP_DIR / f"{_dedup_key(title, message, user_key)}.txt"
    marker.unlink(missing_ok=True)


def _mark_sent(title: str, message: str, user_key: str = "") -> None:
    """Write the dedup marker (used by tests; production code uses _try_claim_send)."""
    PUSHOVER_DEDUP_DIR.mkdir(parents=True, exist_ok=True)
    marker = PUSHOVER_DEDUP_DIR / f"{_dedup_key(title, message, user_key)}.txt"
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        marker.touch()


def execute(
    code: str | None,
    args: list[str] | None,
    kwargs: dict[str, str] | None,
) -> Message:
    if code is not None and args is not None:
        title = args[0]
        message = code
    elif kwargs is not None:
        title = kwargs.get("title", "No title")
        message = kwargs.get("message", "No message")
    else:
        return Message("system", "Tool call failed. Missing parameters!")

    force = kwargs.get("force", "false").lower() == "true" if kwargs else False
    user_key = PUSHOVER_USER_KEY or ""

    # Claim the send slot BEFORE sending so concurrent callers are blocked.
    if not force and not _try_claim_send(title, message, user_key):
        return Message(
            "system",
            "Notification already sent within the last 30 minutes (dedup). "
            "Use force=true to override.",
        )

    url = "https://api.pushover.net/1/messages.json"
    payload = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": message,
        "title": title,
    }
    try:
        response = requests.post(url, data=payload, timeout=30)

        if response.status_code == 200:
            return Message("system", "Notification sent successfully")
        else:
            # Release claim so a future retry is not suppressed.
            if not force:
                _release_claim(title, message, user_key)
            return Message("system", "The notification couldn't be sent")
    except Exception as e:
        if not force:
            _release_claim(title, message, user_key)
        return Message(
            "system", f"Something went wrong while sending the notification: {e}"
        )


def examples(tool_format):
    return f"""
> User: Send me a notification.
> Assistant:
{ToolUse("send_notification", ["This is a test notification!"], "Success").to_output(tool_format)}
> System: Notification sent successfully.
> Assistant: The notification has been sent.
""".strip()


tool = ToolSpec(
    name="notification",
    desc="Send a notification via Pushover push notification service.",
    instructions="Use this tool to send notifications to the user's Pushover account.",
    examples=examples,
    execute=execute,
    block_types=["notification"],
    available=has_pushover_conf(),
    parameters=[
        Parameter(
            name="message",
            type="string",
            description="The message to send in the notification.",
            required=True,
        ),
        Parameter(
            name="title",
            type="string",
            description="The title of the notification.",
            required=False,
        ),
        Parameter(
            name="force",
            type="string",
            description="Set to 'true' to bypass the 30-minute dedup window.",
            required=False,
        ),
    ],
)
