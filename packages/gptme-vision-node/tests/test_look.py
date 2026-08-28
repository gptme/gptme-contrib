"""Tests for the LLM look tool (httpx mocked, no network)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from gptme_vision_node import look
from gptme_vision_node.look import build_request, describe_frame

from .conftest import scene_frame

FRAME = scene_frame(42)


def test_build_request_shape(monkeypatch):
    monkeypatch.delenv("VISION_MODEL", raising=False)
    body = build_request(FRAME, "what is this?")
    assert body["model"] == "gpt-4o-mini"
    (message,) = body["messages"]
    assert message["role"] == "user"
    text_part, image_part = message["content"]
    assert text_part == {"type": "text", "text": "what is this?"}
    url = image_part["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    # The payload must be valid base64 of a JPEG (SOI marker).
    jpeg = base64.b64decode(url.split(",", 1)[1])
    assert jpeg[:2] == b"\xff\xd8"


def test_build_request_model_env(monkeypatch):
    monkeypatch.setenv("VISION_MODEL", "some-vlm")
    assert build_request(FRAME)["model"] == "some-vlm"
    assert build_request(FRAME, model="explicit-wins")["model"] == "explicit-wins"


def test_describe_frame_posts_parses_and_closes_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example/v1/")
    captured = {}
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "a synthetic room"}}]},
        request=httpx.Request("POST", "https://llm.example/v1/chat/completions"),
    )

    def fake_post(url, *, json=None, headers=None, timeout=None):
        captured.update(url=url, body=json, headers=headers)
        return response

    monkeypatch.setattr(look.httpx, "post", fake_post)
    result = describe_frame(FRAME, "describe", model="test-model")

    assert result == "a synthetic room"
    assert response.is_closed
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"]["model"] == "test-model"
    # Request body must be JSON-serializable end to end.
    json.dumps(captured["body"])


def test_describe_frame_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        describe_frame(FRAME)


def test_describe_frame_bad_response_shape(monkeypatch):
    def fake_post(url, **kwargs):
        return httpx.Response(
            200, json={"weird": True}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(look.httpx, "post", fake_post)
    with pytest.raises(ValueError, match="unexpected response"):
        describe_frame(FRAME, api_key="sk-test")


def test_describe_frame_http_error(monkeypatch):
    def fake_post(url, **kwargs):
        return httpx.Response(
            401, json={"error": "nope"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(look.httpx, "post", fake_post)
    with pytest.raises(httpx.HTTPStatusError):
        describe_frame(FRAME, api_key="sk-bad")
