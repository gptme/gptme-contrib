"""Host-side vision tool and event bridge for BobBrain voice sessions."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_PROMPT = "Describe what you see in this image, briefly."
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_ALLOWED_EVENTS = frozenset({"person_appeared", "person_left", "motion"})

SendMessage = Callable[[str], Awaitable[None]]
EventCallback = Callable[[str], Awaitable[None] | None]


def vision_tool_schema() -> dict[str, Any]:
    """Realtime function schema for an on-demand camera look."""
    return {
        "type": "function",
        "name": "look",
        "description": (
            "Look through the connected BobBrain camera and answer a visual "
            "question. Use this whenever the caller asks what you see, who is "
            "there, or asks about something currently in front of the camera."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What to inspect or describe in the current view.",
                }
            },
        },
    }


async def describe_image(
    image: bytes,
    prompt: str = DEFAULT_PROMPT,
    *,
    media_type: str = "image/jpeg",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Describe a JPEG via an OpenAI-compatible chat-completions endpoint."""
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    if media_type != "image/jpeg":
        raise ValueError(f"unsupported image media type: {media_type}")
    encoded = base64.b64encode(image).decode("ascii")
    body = {
        "model": model or os.environ.get("VISION_MODEL", DEFAULT_MODEL),
        "max_tokens": 500,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                    },
                ],
            }
        ],
    }
    url = (base_url or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
        response.raise_for_status()
        data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected response shape: {data!r}") from exc
    if not isinstance(content, str):
        raise ValueError(f"unexpected response content type: {type(content)}")
    return content


@dataclass
class _PendingLook:
    future: asyncio.Future[tuple[bytes, str]]
    created_at: float


class VisionSessionBridge:
    """Coordinate one voice WebSocket's vision events and look requests."""

    def __init__(
        self,
        send_message: SendMessage,
        *,
        on_event: EventCallback | None = None,
        look_timeout_s: float = 8.0,
        describe: Callable[..., Awaitable[str]] = describe_image,
    ) -> None:
        self.send_message = send_message
        self.on_event = on_event
        self.look_timeout_s = look_timeout_s
        self.describe = describe
        self._pending: dict[str, _PendingLook] = {}

    async def look(self, prompt: str = DEFAULT_PROMPT) -> dict[str, str]:
        """Request a current frame from the edge node and describe it remotely."""
        request_id = secrets.token_urlsafe(12)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bytes, str]] = loop.create_future()
        self._pending[request_id] = _PendingLook(future, time.monotonic())
        try:
            await self.send_message(
                json.dumps(
                    {
                        "type": "vision_look_request",
                        "request_id": request_id,
                    }
                )
            )
            image, media_type = await asyncio.wait_for(
                future, timeout=self.look_timeout_s
            )
            description = await self.describe(
                image, prompt or DEFAULT_PROMPT, media_type=media_type
            )
            return {"status": "ok", "description": description}
        except asyncio.TimeoutError:
            return {"error": "Camera did not return a frame before the timeout."}
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            logger.warning("vision look failed: %s", exc)
            return {"error": f"Could not inspect the camera frame: {exc}"}
        finally:
            pending = self._pending.pop(request_id, None)
            if pending is not None and not pending.future.done():
                pending.future.cancel()

    async def handle_message(self, data: object) -> bool:
        """Consume a vision protocol message from the edge node."""
        if not isinstance(data, dict):
            return False
        message_type = data.get("type")
        if message_type == "vision_look_result":
            self._handle_look_result(data)
            return True
        if message_type == "vision_event":
            await self._handle_event(data)
            return True
        return False

    def _handle_look_result(self, data: dict[str, object]) -> None:
        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            return
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            logger.debug("ignoring stale vision result %s", request_id)
            return
        error = data.get("error")
        if isinstance(error, str) and error:
            pending.future.set_exception(RuntimeError(error))
            return
        encoded = data.get("image")
        media_type = data.get("media_type", "image/jpeg")
        if not isinstance(encoded, str) or not isinstance(media_type, str):
            pending.future.set_exception(ValueError("malformed vision image result"))
            return
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError:
            pending.future.set_exception(ValueError("invalid vision image encoding"))
            return
        if not image or len(image) > _MAX_IMAGE_BYTES:
            pending.future.set_exception(ValueError("vision image size is invalid"))
            return
        pending.future.set_result((image, media_type))

    async def _handle_event(self, data: dict[str, object]) -> None:
        event = data.get("event")
        if not isinstance(event, str) or event not in _ALLOWED_EVENTS:
            logger.warning("ignoring unknown vision event: %r", event)
            return
        if self.on_event is None:
            return
        result = self.on_event(event)
        if asyncio.iscoroutine(result):
            await result

    def close(self) -> None:
        """Cancel look requests left behind by a disconnected node."""
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()
