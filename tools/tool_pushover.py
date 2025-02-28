import os

import requests
from gptme.message import Message
from gptme.tools import Parameter, ToolSpec, ToolUse
from gptme.tools.base import ConfirmFunc

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN")


def has_pushover_conf():
    return PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN


def execute(
    code: str | None,
    args: list[str] | None,
    kwargs: dict[str, str] | None,
    confirm: ConfirmFunc,
) -> Message:
    if code is not None and args is not None:
        title = args[0]
        message = code
    elif kwargs is not None:
        title = kwargs.get("title", "No title")
        message = kwargs.get("message", "No message")
    else:
        return Message("system", "Tool call failed. Missing parameters!")

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
            return Message("system", "The notification couldn't be sent")
    except Exception as e:
        return Message("system", f"Something went wrong while sending the notification: {e}")


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
    ],
)
