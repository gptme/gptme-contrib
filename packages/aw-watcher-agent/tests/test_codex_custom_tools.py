"""Mixed Codex tool formats and cursor upgrades must not lose or replay work."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aw_watcher_agent import core, tailer
from aw_watcher_agent.client import AWClient, AWClientError, Event


def _record(kind: str, call_id: str, second: int, **extra: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "timestamp": f"2026-11-26T14:00:{second:02d}Z",
            "payload": {"type": kind, "call_id": call_id, **extra},
        }
    )


def _pair(kind: str, call_id: str, second: int, output: str = "ok") -> list[str]:
    return [
        _record(kind, call_id, second, name=call_id),
        _record(f"{kind}_output", call_id, second + 1, output=output),
    ]


class _Client(AWClient):
    def __init__(self, fail_on: int | None = None) -> None:
        super().__init__("http://fake.invalid")
        self.events: list[Event] = []
        self.fail_on = fail_on

    def ensure_bucket(self, *args: object) -> bool:
        return True

    def heartbeat(self, bucket_id: str, event: Event, pulsetime: float) -> int:
        if len(self.events) + 1 == self.fail_on:
            raise AWClientError("temporary failure")
        self.events.append(event)
        return len(self.events)


@pytest.fixture
def rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    return tmp_path / "rollout-custom.jsonl"


def test_mixed_formats_pair_status_duration_and_private_metadata() -> None:
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "session"}}),
        *_pair("custom_tool_call", "apply_patch", 0, "Process exited with code 0"),
        *_pair("function_call", "exec_command", 2, "Process exited with code 1"),
        *_pair("custom_tool_call", "other", 4),
    ]
    session_id, activities = tailer.parse_rollout(lines)
    assert session_id == "session"
    assert [(a.tool, a.status, a.duration_ms, a.paired) for a in activities] == [
        ("apply_patch", "success", 1000, True),
        ("exec_command", "error", 1000, True),
        ("other", "completed", 1000, True),
    ]
    for activity in activities:
        assert "call_id" not in activity.to_event().data
        assert "call_kind" not in activity.to_event().data


@pytest.mark.parametrize(
    ("legacy_index", "expected"),
    [
        (0, ["custom-between", "function-2", "custom-new", "function-3"]),
        (1, ["custom-new", "function-3"]),
    ],
)
def test_legacy_cursor_maps_function_ordinal_without_replaying(
    rollout: Path, legacy_index: int, expected: list[str]
) -> None:
    lines = [
        *_pair("custom_tool_call", "custom-before", 0),
        *_pair("function_call", "function-1", 2),
        *_pair("custom_tool_call", "custom-between", 4),
        *_pair("function_call", "function-2", 6),
        *_pair("custom_tool_call", "custom-new", 8),
        *_pair("function_call", "function-3", 10),
    ]
    rollout.write_text("\n".join(lines), encoding="utf-8")
    tailer._write_cursor(rollout, legacy_index, "2026-11-26T14:00:02Z")
    client = _Client()
    assert tailer.emit_file(client, "host", rollout) == len(expected)
    assert [event.data["tool"] for event in client.events] == expected
    cursor = tailer._read_cursor(rollout)
    assert cursor is not None
    assert cursor["last_call_id"] == "function-3"
    assert tailer.emit_file(client, "host", rollout) == 0


def test_legacy_cursor_is_not_rewritten_without_new_paired_activity(rollout: Path) -> None:
    rollout.write_text(
        "\n".join(
            [
                *_pair("custom_tool_call", "old-custom", 0),
                *_pair("function_call", "old-function", 2),
                _record("custom_tool_call", "in-flight", 4, name="apply_patch"),
            ]
        ),
        encoding="utf-8",
    )
    tailer._write_cursor(rollout, 0, "2026-11-26T14:00:02Z")
    before = tailer._cursor_path(rollout).read_bytes()
    assert tailer.emit_file(_Client(), "host", rollout) == 0
    assert tailer._cursor_path(rollout).read_bytes() == before


def test_missing_cursor_anchor_preserves_checkpoint_without_emitting(rollout: Path) -> None:
    rollout.write_text(
        "\n".join(
            [
                *_pair("function_call", "unrelated-function", 0),
                *_pair("custom_tool_call", "unrelated-custom", 2),
            ]
        ),
        encoding="utf-8",
    )
    tailer._write_cursor(rollout, 0, "2026-11-26T14:00:00Z", "missing-anchor")
    before = tailer._cursor_path(rollout).read_bytes()
    client = _Client()
    assert tailer.emit_file(client, "host", rollout) == 0
    assert client.events == []
    assert tailer._cursor_path(rollout).read_bytes() == before


def test_custom_inflight_waits_for_late_output(rollout: Path) -> None:
    lines = [
        _record("custom_tool_call", "slow", 0, name="apply_patch"),
        *_pair("function_call", "fast", 2),
    ]
    rollout.write_text("\n".join(lines), encoding="utf-8")
    client = _Client()
    now = datetime(2026, 11, 26, 14, 0, 5, tzinfo=timezone.utc)
    assert tailer.emit_file(client, "host", rollout, now=now) == 0
    assert tailer._read_cursor(rollout) is None
    with rollout.open("a", encoding="utf-8") as stream:
        stream.write("\n" + _record("custom_tool_call_output", "slow", 8, output="ok"))
    assert tailer.emit_file(client, "host", rollout, now=now) == 2
    assert [event.duration for event in client.events] == [8, 1]
    assert tailer.emit_file(client, "host", rollout, now=now) == 0


def test_mixed_partial_failure_checkpoints_each_accepted_heartbeat(rollout: Path) -> None:
    rollout.write_text(
        "\n".join(
            [
                *_pair("custom_tool_call", "accepted", 0),
                *_pair("function_call", "retry", 2),
                *_pair("custom_tool_call", "remaining", 4),
            ]
        ),
        encoding="utf-8",
    )
    first = _Client(fail_on=2)
    with pytest.raises(AWClientError):
        tailer.emit_file(first, "host", rollout)
    assert [event.data["tool"] for event in first.events] == ["accepted"]
    cursor = tailer._read_cursor(rollout)
    assert cursor is not None
    assert cursor["last_call_id"] == "accepted"
    retry = _Client()
    assert tailer.emit_file(retry, "host", rollout) == 2
    assert [event.data["tool"] for event in retry.events] == ["retry", "remaining"]
