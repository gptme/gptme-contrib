"""Exercise the direct judge against the installed SDK without network I/O."""

import json
from unittest.mock import patch

import pytest

from gptme_sessions.judge import _judge_via_anthropic_direct


@pytest.mark.parametrize(
    ("model", "temperature", "expected_temperature"),
    [("claude-haiku-4-5", 0.3, 0.3), ("claude-3-7-sonnet-latest", 0.3, 1.0)],
)
def test_direct_judge_serializes_temperature_with_real_sdk(
    model: str, temperature: float, expected_temperature: float
) -> None:
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx2")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": '{"score": 0.75, "reason": "Good work"}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http_client,
        anthropic.Anthropic(api_key="dummy", http_client=http_client) as client,
        patch.object(anthropic, "Anthropic", return_value=client),
    ):
        result = _judge_via_anthropic_direct(
            "test prompt", model=model, api_key="dummy", temperature=temperature
        )

    assert result is not None
    assert result["score"] == 0.75
    assert len(requests) == 1
    assert requests[0]["temperature"] == expected_temperature
    assert requests[0]["model"] == model
    assert requests[0]["messages"] == [{"role": "user", "content": "test prompt"}]
