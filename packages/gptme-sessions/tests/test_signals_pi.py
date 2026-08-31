"""Pi native v3 session parser regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptme_sessions.pi import PiSessionFormatError
from gptme_sessions.signals import (
    detect_format,
    extract_from_path,
    extract_signals_pi,
    extract_usage_pi,
    parse_trajectory,
)
from gptme_sessions.transcript import read_transcript

FIXTURES = Path(__file__).parent / "fixtures" / "pi"
PRODUCTIVE = FIXTURES / "productive-codex.jsonl"
NOOP_CODEX = FIXTURES / "noop-codex.jsonl"
NOOP_XAI = FIXTURES / "noop-xai.jsonl"


def _header(*, version: int = 3) -> dict:
    return {
        "type": "session",
        "version": version,
        "id": "pi-test",
        "timestamp": "2026-08-31T12:00:00Z",
        "cwd": "/workspace/test",
    }


def _usage(tokens: int, *, cost: float = 0.0) -> dict:
    return {
        "input": tokens,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoning": 0,
        "totalTokens": tokens,
        "cost": {
            "input": cost,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": cost,
        },
    }


def test_real_productive_fixture_is_pi_and_productive() -> None:
    records = parse_trajectory(PRODUCTIVE)
    assert detect_format(records) == "pi"

    result = extract_from_path(PRODUCTIVE)
    assert result["format"] == "pi"
    assert result["productive"] is True
    assert result["grade"] > 0.1
    assert result["tool_calls"] == {"write": 1, "bash": 1}
    assert result["file_writes"] == ["/workspace/pi-fixture-productive/artifact.txt"]
    assert result["git_commits"] == ["test: create Pi fixture (526d692)"]
    assert result["error_count"] == 0

    usage = result["usage"]
    assert usage["provider"] == "openai-codex"
    assert usage["model"] == "gpt-5.6-luna"
    assert usage["stop_reason"] == "stop"
    assert usage["input_tokens"] == 1154
    assert usage["output_tokens"] == 163
    assert usage["total_tokens"] == 1317
    assert usage["reasoning_tokens"] == 27
    assert usage["cost"] == pytest.approx(0.0004264)


@pytest.mark.parametrize(
    ("fixture", "provider", "model", "total_tokens", "cache_read", "cost"),
    [
        (NOOP_CODEX, "openai-codex", "gpt-5.6-luna", 48, 0, 0.0000196),
        (NOOP_XAI, "xai", "grok-4.6", 712, 512, 0.000824),
    ],
)
def test_real_smoke_fixture_remains_truthful_noop(
    fixture: Path,
    provider: str,
    model: str,
    total_tokens: int,
    cache_read: int,
    cost: float,
) -> None:
    result = extract_from_path(fixture)
    assert result["format"] == "pi"
    assert result["productive"] is False
    assert result["grade"] == 0.1
    assert result["tool_calls"] == {}
    assert result["file_writes"] == []
    assert result["git_commits"] == []
    assert result["deliverables"] == []
    assert result["usage"]["provider"] == provider
    assert result["usage"]["model"] == model
    assert result["usage"]["stop_reason"] == "stop"
    assert result["usage"]["total_tokens"] == total_tokens
    assert result["usage"]["cache_read_tokens"] == cache_read
    assert result["usage"]["cost"] == pytest.approx(cost)


def test_productive_fixture_transcript_and_metadata() -> None:
    transcript = read_transcript(PRODUCTIVE)
    assert transcript.harness == "pi"
    assert transcript.session_id == "pi-fixture-productive-codex-001"
    assert transcript.session_name == "pi-fixture-productive-codex-001"
    assert transcript.project == "/workspace/pi-fixture-productive"
    assert transcript.provider == "openai-codex"
    assert transcript.model == "gpt-5.6-luna"
    assert transcript.stop_reason == "stop"
    assert transcript.cost == pytest.approx(0.0004264)
    assert transcript.usage is not None
    assert transcript.usage["total_tokens"] == 1317
    assert [message.tool_name for message in transcript.messages if message.tool_name] == [
        "write",
        "bash",
    ]
    text = "\n".join(message.content for message in transcript.messages)
    assert "[redacted]" not in text
    assert "526d69201a4b1581eff6a83a3d94fc44bbf7e871" in text


def test_branch_side_effects_count_but_transcript_follows_active_leaf() -> None:
    records = [
        _header(),
        {
            "type": "message",
            "id": "root",
            "parentId": None,
            "timestamp": "2026-08-31T12:00:01Z",
            "message": {"role": "user", "content": "do work", "timestamp": 1},
        },
        {
            "type": "message",
            "id": "abandoned-call",
            "parentId": "root",
            "timestamp": "2026-08-31T12:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-branch",
                        "name": "bash",
                        "arguments": {"command": "git commit -m 'fix: branch work'"},
                    }
                ],
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "usage": _usage(10),
                "stopReason": "toolUse",
            },
        },
        {
            "type": "message",
            "id": "abandoned-result",
            "parentId": "abandoned-call",
            "timestamp": "2026-08-31T12:00:03Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-branch",
                "toolName": "bash",
                "content": [{"type": "text", "text": "[topic abc1234] fix: branch work\n"}],
                "isError": False,
            },
        },
        {
            "type": "message",
            "id": "active-leaf",
            "parentId": "root",
            "timestamp": "2026-08-31T12:00:04Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Used another branch."}],
                "provider": "xai",
                "model": "grok-4.6",
                "usage": _usage(5),
                "stopReason": "stop",
            },
        },
    ]

    signals = extract_signals_pi(records)
    assert signals["git_commits"] == ["fix: branch work (abc1234)"]
    assert signals["tool_calls"] == {"bash": 1}
    usage = extract_usage_pi(records)
    assert usage["total_tokens"] == 15
    assert usage["provider"] == "xai"
    assert usage["model"] == "grok-4.6"
    assert usage["stop_reason"] == "stop"

    fixture = FIXTURES / "_not_written.jsonl"
    # The normalizer itself takes parsed records; avoid creating a throwaway fixture.
    from gptme_sessions.transcript import _normalize_pi

    transcript_messages = _normalize_pi(records)
    assert not fixture.exists()
    assert all(message.tool_name != "bash" for message in transcript_messages)
    assert any("another branch" in message.content for message in transcript_messages)


def test_compaction_retained_tail_usage_is_not_double_counted() -> None:
    records = [
        _header(),
        {
            "type": "message",
            "id": "root",
            "parentId": None,
            "timestamp": "2026-08-31T12:00:01Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "before"}],
                "usage": _usage(10),
                "stopReason": "stop",
            },
        },
        {
            "type": "compaction",
            "id": "compact",
            "parentId": "root",
            "timestamp": "2026-08-31T12:00:02Z",
            "summary": "summary",
            "tokensBefore": 999,
            "usage": _usage(5),
            "retainedTail": [{"role": "assistant", "usage": _usage(999)}],
        },
        {
            "type": "message",
            "id": "leaf",
            "parentId": "compact",
            "timestamp": "2026-08-31T12:00:03Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "after"}],
                "usage": _usage(2),
                "stopReason": "stop",
            },
        },
    ]
    assert extract_usage_pi(records)["total_tokens"] == 17
    assert extract_signals_pi(records)["compaction_count"] == 1


def test_pi_print_stream_is_detected_then_rejected() -> None:
    records = [_header(), {"type": "agent_start"}]
    with pytest.raises(PiSessionFormatError, match="entry type 'agent_start'"):
        detect_format(records)


def test_future_pi_version_fails_visibly() -> None:
    records = [_header(version=4)]
    with pytest.raises(PiSessionFormatError, match="version 4"):
        detect_format(records)


def test_malformed_pi_json_fails_visibly(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text(json.dumps(_header()) + "\n{" + "\n", encoding="utf-8")
    with pytest.raises(PiSessionFormatError, match="invalid JSON.*line 2"):
        parse_trajectory(path)


@pytest.mark.parametrize(
    "invalid_value",
    ["NaN", "[" * 10000 + "0" + "]" * 10000],
    ids=["nonfinite", "recursive"],
)
def test_strict_pi_json_failures_fail_visibly(tmp_path: Path, invalid_value: str) -> None:
    path = tmp_path / "strict-invalid.jsonl"
    path.write_text(
        json.dumps(_header())
        + "\n"
        + '{"type":"custom","id":"bad","parentId":null,'
        + '"timestamp":"2026-08-31T12:00:01Z","value":'
        + invalid_value
        + "}\n",
        encoding="utf-8",
    )

    with pytest.raises(PiSessionFormatError, match="invalid JSON.*line 2"):
        parse_trajectory(path)


def test_provider_error_and_failed_tool_are_errors() -> None:
    records = [
        _header(),
        {
            "type": "message",
            "id": "call",
            "parentId": None,
            "timestamp": "2026-08-31T12:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "bad",
                        "name": "bash",
                        "arguments": {"command": "false"},
                    }
                ],
                "usage": _usage(1),
                "stopReason": "error",
                "errorMessage": "provider failed",
            },
        },
        {
            "type": "message",
            "id": "result",
            "parentId": "call",
            "timestamp": "2026-08-31T12:00:02Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "bad",
                "toolName": "bash",
                "content": [{"type": "text", "text": "failed"}],
                "isError": True,
            },
        },
    ]
    signals = extract_signals_pi(records)
    assert signals["error_count"] == 2
    assert signals["provider_errors"] == ["provider failed"]
    assert signals["stop_reason"] == "error"
