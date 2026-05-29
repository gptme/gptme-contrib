"""Tests for aw-watcher-agent: core conventions, state, and the REST client."""

from __future__ import annotations

from pathlib import Path

import pytest

from aw_watcher_agent import core
from aw_watcher_agent.client import AWClient, AWClientError, Event


# --- core ------------------------------------------------------------------


def test_bucket_id_convention():
    assert core.bucket_id("bob") == "aw-watcher-agent_bob"


def test_session_data_drops_empty_and_adds_outcome():
    args = {
        "harness": "claude-code",
        "model": "claude-opus-4-7",
        "category": "",  # dropped
        "session_id": "8531",
        "trigger": None,  # dropped
        "workspace": "bob",
    }
    start = core.session_data(args)
    assert start == {
        "harness": "claude-code",
        "model": "claude-opus-4-7",
        "session_id": "8531",
        "workspace": "bob",
    }
    assert "outcome" not in start
    end = core.session_data(args, outcome="productive")
    assert end["outcome"] == "productive"


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert core.read_state("abc") is None
    core.write_state("abc", {"bucket_id": "b", "event_id": 7, "start": "t"})
    assert core.read_state("abc") == {"bucket_id": "b", "event_id": 7, "start": "t"}
    core.clear_state("abc")
    assert core.read_state("abc") is None


def test_state_path_sanitizes_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = core.state_path("a/b c")
    assert "/" not in path.name
    assert path.name == "session-a_b_c.json"


# --- client (mocked transport) ---------------------------------------------


class _FakeClient(AWClient):
    """AWClient with ``_request`` stubbed by a scripted response queue."""

    def __init__(self, responses):
        super().__init__("http://test")
        self._responses = list(responses)
        self.calls = []

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self._responses.pop(0)


def test_ensure_bucket_creates_when_missing():
    c = _FakeClient([(200, {}), (200, None)])  # buckets(), POST create
    assert c.ensure_bucket("aw-watcher-agent_bob", "app.agent.session", "x", "bob") is True
    assert c.calls[1][0] == "POST"


def test_ensure_bucket_idempotent_when_present():
    c = _FakeClient([(200, {"aw-watcher-agent_bob": {}})])
    assert c.ensure_bucket("aw-watcher-agent_bob", "app.agent.session", "x", "bob") is False
    assert len(c.calls) == 1  # no POST


def test_post_event_returns_id_from_dict():
    c = _FakeClient([(200, {"id": 42, "timestamp": "t", "duration": 0})])
    eid = c.post_event("b", Event("t", 0.0, {"harness": "gptme"}))
    assert eid == 42
    assert c.calls[0] == (
        "POST",
        "/api/0/buckets/b/events",
        {"timestamp": "t", "duration": 0.0, "data": {"harness": "gptme"}},
    )


def test_post_event_returns_id_from_list():
    c = _FakeClient([(201, [{"id": 9}])])
    assert c.post_event("b", Event("t", 1.0, {})) == 9


def test_post_event_raises_on_error():
    c = _FakeClient([(500, "boom")])
    with pytest.raises(AWClientError):
        c.post_event("b", Event("t", 0.0, {}))


def test_delete_event():
    c = _FakeClient([(200, None)])
    assert c.delete_event("b", 5) is True
    assert c.calls[0] == ("DELETE", "/api/0/buckets/b/events/5", None)


def test_info_validates_shape():
    c = _FakeClient([(200, {"hostname": "bob"})])
    assert c.info()["hostname"] == "bob"
    c2 = _FakeClient([(200, "not-a-dict")])
    with pytest.raises(AWClientError):
        c2.info()


# --- CLI emit-start / emit-end metadata persistence -------------------------


def test_emit_end_preserves_start_metadata(tmp_path, monkeypatch):
    """emit-end must carry harness/model/workspace even when not re-supplied."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from aw_watcher_agent import cli

    # Simulate what emit-start writes into the state file.
    saved_data = {
        "harness": "claude-code",
        "model": "claude-opus-4-7",
        "session_id": "abc1",
        "workspace": "bob",
    }
    core.write_state(
        "abc1",
        {
            "bucket_id": "aw-watcher-agent_host",
            "event_id": 42,
            "start": "2026-01-01T00:00:00+00:00",
            "data": saved_data,
        },
    )

    # emit-end is called with only --session-id and --outcome (the typical case).
    import argparse

    args = argparse.Namespace(
        server="http://localhost:5600",
        hostname="host",  # matches the bucket_id saved in state above
        session_id="abc1",
        outcome="productive",
        harness=None,
        model=None,
        category=None,
        trigger=None,
        workspace=None,
        duration=None,
        strict=False,
    )

    posted_events: list[dict] = []

    class _CapturingClient(_FakeClient):
        def __init__(self):
            # ensure_bucket check, delete placeholder, heartbeat for final event
            super().__init__(
                [
                    (200, {"aw-watcher-agent_host": {}}),  # ensure_bucket
                    (200, None),  # delete_event
                    (200, {"id": 99, "timestamp": "t", "duration": 0}),  # heartbeat
                ]
            )

        def heartbeat(self, bid, event, pulsetime):
            posted_events.append(event.data)
            return super().heartbeat(bid, event, pulsetime)

    monkeypatch.setattr(cli, "AWClient", lambda _url: _CapturingClient())
    rc = cli.cmd_emit_end(args)
    assert rc == 0
    assert posted_events, "no heartbeat was posted"
    final_data = posted_events[-1]
    assert final_data.get("harness") == "claude-code", "harness lost from final event"
    assert final_data.get("model") == "claude-opus-4-7", "model lost from final event"
    assert final_data.get("workspace") == "bob", "workspace lost from final event"
    assert final_data.get("outcome") == "productive"


# --- Codex log-tailer (Phase 2) ---------------------------------------------

import json as _json  # noqa: E402

from aw_watcher_agent import tailer  # noqa: E402


def _rollout_lines(records):
    return [_json.dumps(r) for r in records]


def _call(call_id, name, ts):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "function_call", "name": name, "call_id": call_id},
    }


def _output(call_id, ts, output):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def test_activity_bucket_id_is_sibling():
    assert core.activity_bucket_id("bob") == "aw-watcher-agent-activity_bob"
    assert core.activity_bucket_id("bob") != core.bucket_id("bob")


def test_parse_rollout_pairs_calls_with_outputs():
    lines = _rollout_lines(
        [
            {"type": "session_meta", "payload": {"id": "sess-1", "type": None}},
            _call("c1", "exec_command", "2026-05-29T05:19:56.055Z"),
            _output("c1", "2026-05-29T05:19:58.265Z", "Process exited with code 0\nOutput:\nok"),
            _call("c2", "apply_patch", "2026-05-29T05:20:00.000Z"),
            _output("c2", "2026-05-29T05:20:00.500Z", "exited with code 1\nboom"),
        ]
    )
    session_id, acts = tailer.parse_rollout(lines)
    assert session_id == "sess-1"
    assert [a.tool for a in acts] == ["exec_command", "apply_patch"]
    assert acts[0].status == "success"
    assert acts[0].duration_ms == 2210  # 58.265 - 56.055 = 2.210s
    assert acts[0].session_id == "sess-1"
    assert acts[1].status == "error"
    assert acts[1].duration_ms == 500


def test_parse_rollout_unpaired_call_gets_zero_duration():
    lines = _rollout_lines([_call("c9", "read_file", "2026-05-29T05:19:56.055Z")])
    _, acts = tailer.parse_rollout(lines)
    assert len(acts) == 1
    assert acts[0].duration_ms == 0
    assert acts[0].status == "completed"


def test_parse_rollout_ignores_non_tool_records():
    lines = _rollout_lines(
        [
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
            {"type": "turn_context", "payload": None},
            "not json at all",
        ]
    )
    session_id, acts = tailer.parse_rollout(lines)
    assert session_id == ""
    assert acts == []


def test_status_from_output_variants():
    assert tailer._status_from_output("Process exited with code 0") == "success"
    assert tailer._status_from_output("exited with code 127") == "error"
    assert tailer._status_from_output("some normal tool result") == "completed"
    assert tailer._status_from_output("") == "completed"
    assert tailer._status_from_output(None) == "completed"


def test_activity_to_event_excludes_duration_from_data():
    act = tailer.ToolActivity(
        tool="exec_command",
        status="success",
        duration_ms=1500,
        timestamp="2026-05-29T05:19:56.055Z",
        call_id="c1",
        session_id="sess-1",
    )
    ev = act.to_event()
    assert ev.data == {"tool": "exec_command", "status": "success", "session_id": "sess-1"}
    assert ev.duration == 1.5
    assert "duration_ms" not in ev.data  # keeps same-tool/status blocks mergeable


def test_emit_file_ensures_bucket_and_heartbeats(tmp_path):
    rollout = tmp_path / "rollout-test.jsonl"
    rollout.write_text(
        "\n".join(
            _rollout_lines(
                [
                    {"type": "session_meta", "payload": {"id": "sess-1", "type": None}},
                    _call("c1", "exec_command", "2026-05-29T05:19:56.055Z"),
                    _output("c1", "2026-05-29T05:19:56.255Z", "Process exited with code 0"),
                ]
            )
        ),
        encoding="utf-8",
    )

    seen: list[tuple] = []

    class _Client(_FakeClient):
        def __init__(self):
            super().__init__([(200, {})])  # buckets() lookup for ensure_bucket

        def ensure_bucket(self, bid, etype, client_name, host):
            seen.append(("ensure", bid, etype))
            return True

        def heartbeat(self, bid, event, pulsetime):
            seen.append(("hb", bid, event.data, pulsetime))
            return 1

    count = tailer.emit_file(_Client(), "bob", rollout, pulsetime=3.0)
    assert count == 1
    assert seen[0] == ("ensure", "aw-watcher-agent-activity_bob", "app.agent.activity")
    assert seen[1] == (
        "hb",
        "aw-watcher-agent-activity_bob",
        {"tool": "exec_command", "status": "success", "session_id": "sess-1"},
        3.0,
    )


# ---------------------------------------------------------------------------
# Cursor tests: verify incremental emission prevents duplicate heartbeats.
# ---------------------------------------------------------------------------


def test_cursor_path_is_stable_and_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(tailer.core, "state_dir", lambda: tmp_path)
    p1 = tailer._cursor_path(Path("/tmp/sessions/2026/05/29/rollout-abc.jsonl"))
    p2 = tailer._cursor_path(Path("/tmp/sessions/2026/05/29/rollout-abc.jsonl"))
    p3 = tailer._cursor_path(Path("/tmp/sessions/2026/05/29/rollout-def.jsonl"))
    assert str(p1) == str(p2), "same rollout → same cursor path"
    assert str(p1) != str(p3), "different rollout → different cursor path"


def test_cursor_read_write_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(tailer.core, "state_dir", lambda: tmp_path)
    rollout = Path("/fake/rollout.jsonl")
    tailer._write_cursor(rollout, 5, "2026-05-29T05:30:00Z")
    cur = tailer._read_cursor(rollout)
    assert cur is not None
    assert cur["last_index"] == 5
    assert cur["last_timestamp"] == "2026-05-29T05:30:00Z"


def test_cursor_read_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(tailer.core, "state_dir", lambda: tmp_path)
    cur = tailer._read_cursor(Path("/nonexistent/rollout.jsonl"))
    assert cur is None


def test_emit_file_incremental_only_emits_new_calls(tmp_path, monkeypatch):
    """First run emits all 2 calls; second run emits 0 (cursor advanced)."""
    monkeypatch.setattr(tailer.core, "state_dir", lambda: tmp_path)
    rollout = tmp_path / "rollout-test.jsonl"
    rollout.write_text(
        "\n".join(
            _rollout_lines(
                [
                    {"type": "session_meta", "payload": {"id": "sess-1", "type": None}},
                    _call("c1", "exec_command", "2026-05-29T05:19:56.055Z"),
                    _output("c1", "2026-05-29T05:19:56.255Z", "Process exited with code 0"),
                    _call("c2", "apply_patch", "2026-05-29T05:20:00.000Z"),
                    _output("c2", "2026-05-29T05:20:00.500Z", "exited with code 1\nboom"),
                ]
            )
        ),
        encoding="utf-8",
    )

    seen: list[tuple] = []

    class _Client(_FakeClient):
        def __init__(self):
            super().__init__([])

        def ensure_bucket(self, bid, etype, client_name, host):
            seen.append(("ensure", bid))
            return True

        def heartbeat(self, bid, event, pulsetime):
            seen.append(("hb", bid, event.data["tool"]))
            return 1

    # First run: emits both calls, writes cursor.
    count1 = tailer.emit_file(_Client(), "bob", rollout, pulsetime=3.0)
    assert count1 == 2
    assert seen == [
        ("ensure", "aw-watcher-agent-activity_bob"),
        ("hb", "aw-watcher-agent-activity_bob", "exec_command"),
        ("hb", "aw-watcher-agent-activity_bob", "apply_patch"),
    ]

    # Verify cursor was written.
    cur = tailer._read_cursor(rollout)
    assert cur is not None
    assert cur["last_index"] == 1  # 0-based index of the last (2nd) activity
    assert cur["last_timestamp"] == "2026-05-29T05:20:00.000Z"  # start of apply_patch

    # Second run: nothing emitted (file unchanged, cursor already advanced).
    seen.clear()
    count2 = tailer.emit_file(_Client(), "bob", rollout, pulsetime=3.0)
    assert count2 == 0
    assert seen == []  # No ensure, no heartbeats — everything already emitted.


def test_emit_file_incremental_appends_new_calls(tmp_path, monkeypatch):
    """First run emits calls 1-2; after appending a 3rd call, only call 3 emits."""
    monkeypatch.setattr(tailer.core, "state_dir", lambda: tmp_path)
    rollout = tmp_path / "rollout-test.jsonl"

    # Write initial 2-call transcript.
    initial_lines = _rollout_lines(
        [
            {"type": "session_meta", "payload": {"id": "sess-1", "type": None}},
            _call("c1", "exec_command", "2026-05-29T05:19:56.055Z"),
            _output("c1", "2026-05-29T05:19:56.255Z", "Process exited with code 0"),
            _call("c2", "apply_patch", "2026-05-29T05:20:00.000Z"),
            _output("c2", "2026-05-29T05:20:00.500Z", "exited with code 1\nboom"),
        ]
    )
    rollout.write_text("\n".join(initial_lines), encoding="utf-8")

    seen: list[str] = []

    class _Client(_FakeClient):
        def __init__(self):
            super().__init__([])

        def ensure_bucket(self, bid, etype, client_name, host):
            return True

        def heartbeat(self, bid, event, pulsetime):
            seen.append(event.data["tool"])
            return 1

    # First run.
    tailer.emit_file(_Client(), "bob", rollout)
    assert seen == ["exec_command", "apply_patch"]
    seen.clear()

    # Append a 3rd call.
    extra_lines = _rollout_lines(
        [
            _call("c3", "read_file", "2026-05-29T05:21:00.000Z"),
            _output("c3", "2026-05-29T05:21:00.350Z", "Process exited with code 0"),
        ]
    )
    with open(rollout, "a", encoding="utf-8") as f:
        f.write("\n" + "\n".join(extra_lines))

    # Second run: only the new call emits.
    tailer.emit_file(_Client(), "bob", rollout)
    assert seen == ["read_file"], "only new call should emit"


def test_emit_file_does_not_advance_cursor_past_inflight_tail_call(tmp_path, monkeypatch):
    """Cursor stops short of in-flight tail calls; they emit correctly after output arrives."""
    monkeypatch.setattr(tailer.core, "state_dir", lambda: tmp_path)
    rollout = tmp_path / "rollout-inflight.jsonl"

    # c1 is paired; c2 is in-flight (no output yet).
    rollout.write_text(
        "\n".join(
            _rollout_lines(
                [
                    {"type": "session_meta", "payload": {"id": "sess-1", "type": None}},
                    _call("c1", "exec_command", "2026-05-29T05:19:56.055Z"),
                    _output("c1", "2026-05-29T05:19:56.255Z", "Process exited with code 0"),
                    _call("c2", "apply_patch", "2026-05-29T05:20:00.000Z"),
                    # c2 output not yet written — in-flight
                ]
            )
        ),
        encoding="utf-8",
    )

    seen: list[str] = []

    class _Client(_FakeClient):
        def __init__(self):
            super().__init__([])

        def ensure_bucket(self, bid, etype, client_name, host):
            return True

        def heartbeat(self, bid, event, pulsetime):
            seen.append(event.data["tool"])
            return 1

    # Run 1: only c1 emitted; cursor stops before in-flight c2.
    count1 = tailer.emit_file(_Client(), "bob", rollout)
    assert count1 == 1
    assert seen == ["exec_command"]
    cur = tailer._read_cursor(rollout)
    assert cur is not None
    assert cur["last_index"] == 0  # cursor stopped before in-flight c2

    # Simulate c2's output arriving.
    seen.clear()
    with open(rollout, "a", encoding="utf-8") as f:
        f.write(
            "\n"
            + _rollout_lines(
                [_output("c2", "2026-05-29T05:20:00.500Z", "exited with code 1\nboom")]
            )[0]
        )

    # Run 2: c2 now has output and emits with correct status/duration.
    count2 = tailer.emit_file(_Client(), "bob", rollout)
    assert count2 == 1
    assert seen == ["apply_patch"]
    cur = tailer._read_cursor(rollout)
    assert cur is not None
    assert cur["last_index"] == 1  # cursor now covers c2


def test_emit_file_no_cursor_on_empty_activities(tmp_path, monkeypatch):
    """No-op when the rollout file has no tool calls (no cursor written)."""
    monkeypatch.setattr(tailer.core, "state_dir", lambda: tmp_path)
    rollout = tmp_path / "empty.jsonl"
    rollout.write_text(
        "\n".join(
            _rollout_lines([{"type": "session_meta", "payload": {"id": "sess-1", "type": None}}])
        ),
        encoding="utf-8",
    )

    class _Client(_FakeClient):
        def __init__(self):
            super().__init__([])

        def ensure_bucket(self, bid, etype, client_name, host):
            return True

    count = tailer.emit_file(_Client(), "bob", rollout)
    assert count == 0
    # Cursor should NOT be created when there's nothing to emit.
    assert tailer._read_cursor(rollout) is None
