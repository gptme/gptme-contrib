"""The LLM "look" tool: describe a frame via a vision-capable chat model.

Talks to any OpenAI-compatible chat-completions endpoint. Configuration
via env vars:

- ``OPENAI_API_KEY`` — required (unless ``api_key`` is passed)
- ``OPENAI_BASE_URL`` — default ``https://api.openai.com/v1``
- ``VISION_MODEL`` — default ``gpt-4o-mini``

This is the on-demand "see things" toolcall the voice bridge wires into
the realtime session.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import cv2
import httpx
import numpy as np

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_PROMPT = "Describe what you see in this image, briefly."


def encode_frame_jpeg(frame: np.ndarray, *, quality: int = 85) -> bytes:
    """JPEG-encode a BGR frame."""
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise ValueError("failed to JPEG-encode frame")
    return buf.tobytes()


def build_request(
    frame: np.ndarray,
    prompt: str = DEFAULT_PROMPT,
    *,
    model: str | None = None,
    max_tokens: int = 500,
    jpeg_quality: int = 85,
) -> dict[str, Any]:
    """Build the chat-completions request body for a frame + prompt."""
    jpeg = encode_frame_jpeg(frame, quality=jpeg_quality)
    b64 = base64.b64encode(jpeg).decode("ascii")
    return {
        "model": model or os.environ.get("VISION_MODEL", DEFAULT_MODEL),
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
    }


def describe_frame(
    frame: np.ndarray,
    prompt: str = DEFAULT_PROMPT,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Send a frame to a vision LLM and return its description."""
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set (and no api_key passed)")
    url = (base_url or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")

    body = build_request(frame, prompt, model=model)
    response = httpx.post(
        f"{url}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
    )
    try:
        response.raise_for_status()
        data = response.json()
    finally:
        response.close()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"unexpected response shape: {data!r}") from e
    if not isinstance(content, str):
        raise ValueError(f"unexpected response content type: {type(content)}")
    return content
