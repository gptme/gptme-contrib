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


def _dedup_key(title: str, message: str) -> str:
    return hashlib.sha256(f"{title}|{message}".encode()).hexdigest()[:16]


def _is_recent_duplicate(title: str, message: str) -> bool:
    marker = PUSHOVER_DEDUP_DIR / f"{_dedup_key(title, message)}.txt"
    if not marker.exists():
        return False
    return time.time() - marker.stat().st_mtime < PUSHOVER_DEDUP_TTL


def _mark_sent(title: str, message: str) -> None:
    PUSHOVER_DEDUP_DIR.mkdir(parents=True, exist_ok=True)
    (PUSHOVER_DEDUP_DIR / f"{_dedup_key(title, message)}.txt").touch()


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

    if not force and _is_recent_duplicate(title, message):
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
            _mark_sent(title, message)
            return Message("system", "Notification sent successfully")
        else:
            return Message("system", "The notification couldn't be sent")
    except Exception as e:
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
