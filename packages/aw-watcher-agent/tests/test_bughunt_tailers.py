"""Verified-bug-hunt findings for aw-watcher-agent (correctness/ordering/edge input).

Regression tests for the 2026-11-24 verified bug hunt.
No real aw-server or home directory is touched: state_dir is redirected to
tmp_path and the client is a fake.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from aw_watcher_agent import cc_tailer, cli, core, tailer
from aw_watcher_agent.client import AWClient, AWClientError


# --- helpers ---------------------------------------------------------------


def _rec(rtype: str, ts: str, payload: dict) -> str:
    return json.dumps({"timestamp": ts, "type": rtype, "payload": payload}, ensure_ascii=False)


def _meta() -> str:
    return _rec("session_meta", "2026-05-29T05:19:00.000Z", {"id": "sess-1"})


def _call(cid: str, name: str, ts: str, arguments: str = "{}") -> str:
    return _rec(
        "response_item",
        ts,
        {"type": "function_call", "name": name, "call_id": cid, "arguments": arguments},
    )


def _out(cid: str, ts: str, output: str) -> str:
    return _rec(
        "response_item", ts, {"type": "function_call_output", "call_id": cid, "output": output}
    )


class _HBClient(AWClient):
    """Fake activity client: records heartbeats, optionally fails on the Nth one."""

    def __init__(self, fail_on: int | None = None):
        super().__init__("http://fake.invalid")
        self.events: list = []
        self.fail_on = fail_on
        self.calls = 0

    def _request(self, method, path, body=None):  # never touch the network
        raise AssertionError(f"unexpected network call {method} {path}")

    def ensure_bucket(self, bid, etype, client_name, host):
        return True

    def heartbeat(self, bid, event, pulsetime):
        self.calls += 1
        if self.fail_on is not None and self.calls == self.fail_on:
            raise AWClientError("server hiccup")
        self.events.append(event)
        return self.calls


class _PostClient(AWClient):
    def __init__(self):
        super().__init__("http://fake.invalid")
        self.posted: list = []

    def _request(self, method, path, body=None):
        raise AssertionError(f"unexpected network call {method} {path}")

    def ensure_bucket(self, bid, etype, client_name, host):
        return True

    def post_event(self, bucket_id, event):
        self.posted.append(event)
        return len(self.posted)


# --- AC1 -------------------------------------------------------------------


def test_ac2_codex_partial_heartbeat_failure_replays_already_sent_calls(tmp_path, monkeypatch):
    """A heartbeat failure mid-batch leaves the cursor unwritten, so the next
    run re-sends calls that the server already accepted (duplicate events)."""
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    rollout = tmp_path / "rollout-fail.jsonl"
    lines = [
        _meta(),
        _call("c1", "exec_command", "2026-05-29T05:20:00.000Z"),
        _out("c1", "2026-05-29T05:20:01.000Z", "Process exited with code 0"),
        _call("c2", "read_file", "2026-05-29T05:21:00.000Z"),
        _out("c2", "2026-05-29T05:21:01.000Z", "ok"),
    ]
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")

    first = _HBClient(fail_on=2)  # c1 accepted, c2 fails
    try:
        tailer.emit_file(first, "host", rollout)
    except AWClientError:
        pass
    assert [e.data["tool"] for e in first.events] == ["exec_command"]

    second = _HBClient()
    tailer.emit_file(second, "host", rollout)
    # Only c2 is new for the server; c1 was already accepted.
    assert [e.data["tool"] for e in second.events] == ["read_file"]


# --- AC3 -------------------------------------------------------------------


def test_ac3_codex_emit_file_unicode_line_separator(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    rollout = tmp_path / "rollout-u2028.jsonl"
    rollout.write_text(
        "\n".join(
            [
                _meta(),
                _call("c1", "exec_command", "2026-05-29T05:20:00.000Z"),
                _out("c1", "2026-05-29T05:20:02.000Z", "Process exited with code 1 x"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = _HBClient()
    # The call is complete in the file, so it should be emitted once with its status.
    assert tailer.emit_file(client, "host", rollout) == 1
    assert client.events[0].data["status"] == "error"


# --- AC4 -------------------------------------------------------------------


def _cc_row(ts: str, text: str = "hi") -> str:
    rec = {
        "type": "user",
        "timestamp": ts,
        "sessionId": "s1",
        "cwd": "/work/proj",
        "message": {"content": text},
    }
    # Node's JSON.stringify writes U+2028 raw (ensure_ascii=False equivalent).
    return json.dumps(rec, ensure_ascii=False)


def test_ac4_cc_unicode_line_separator_row_dropped_splits_block(tmp_path, monkeypatch):
    """A Claude Code row whose content holds U+2028 is split by splitlines()
    and silently dropped, so one continuous working stretch becomes two
    blocks with a fake idle gap."""
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _cc_row("2026-11-10T09:00:00Z"),
                _cc_row("2026-11-10T09:10:00Z", "pasted text"),
                _cc_row("2026-11-10T09:20:00Z"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = _PostClient()
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    cc_tailer.emit_file(client, "host", transcript, now=now, owner="s1")
    # rows are 10 min apart (< 15 min gap): exactly one 20-minute block.
    assert [e.duration for e in client.posted] == [1200.0]


# --- AC5 -------------------------------------------------------------------


def test_ac5_mixed_naive_aware_timestamps_crash_tail_codex(tmp_path, monkeypatch, capsys):
    """One row with a timezone-less timestamp makes parse_rollout subtract an
    aware from a naive datetime: TypeError escapes main()'s non-fatal handler
    and the whole tail-codex run dies with a traceback."""
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(
        AWClient,
        "_request",
        lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("no network")),
    )
    monkeypatch.setattr(AWClient, "ensure_bucket", lambda self, *a, **k: True)
    monkeypatch.setattr(AWClient, "heartbeat", lambda self, *a, **k: 1)
    rollout = tmp_path / "rollout-tz.jsonl"
    rollout.write_text(
        "\n".join(
            [
                _meta(),
                _call("c1", "exec_command", "2026-05-29T05:20:00.000Z"),
                _out("c1", "2026-05-29T05:20:01.000", "Process exited with code 0"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rc = cli.main(["tail-codex", "--file", str(rollout), "--hostname", "host"])
    assert rc == 0


# --- gptme/gptme-contrib#2366 AC1: out-of-order Codex output -----------------


def _ooo_rollout(tmp_path):
    rollout = tmp_path / "rollout-ooo.jsonl"
    lines = [
        _meta(),
        _call("slow", "exec_command", "2026-11-24T10:00:00.000Z"),
        _call("fast", "read_file", "2026-11-24T10:00:01.000Z"),
        _out("fast", "2026-11-24T10:00:02.000Z", "ok"),
    ]
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rollout


def test_codex_late_output_is_held_back_not_emitted_as_orphan(tmp_path, monkeypatch):
    """An unpaired call ahead of a paired one is held back within the grace
    window, so its late output still pairs instead of leaving a 0 ms stub."""
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    rollout = _ooo_rollout(tmp_path)
    client = _HBClient()
    soon = datetime(2026, 11, 24, 10, 0, 5, tzinfo=timezone.utc)
    assert tailer.emit_file(client, "host", rollout, now=soon) == 0

    with rollout.open("a", encoding="utf-8") as fh:
        fh.write(_out("slow", "2026-11-24T10:00:10.000Z", "Process exited with code 0") + "\n")
    later = datetime(2026, 11, 24, 10, 0, 15, tzinfo=timezone.utc)
    assert tailer.emit_file(client, "host", rollout, now=later) == 2
    assert [e.data["tool"] for e in client.events] == ["exec_command", "read_file"]
    assert client.events[0].duration == 10


def test_codex_genuine_orphan_still_emits_after_grace(tmp_path, monkeypatch):
    """A call whose output never arrives is emitted as a crash orphan once the
    grace window has passed, so it cannot block later calls forever."""
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    rollout = _ooo_rollout(tmp_path)
    client = _HBClient()
    after_grace = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc) + tailer.ORPHAN_GRACE
    assert tailer.emit_file(client, "host", rollout, now=after_grace) == 2
    assert [e.data["tool"] for e in client.events] == ["exec_command", "read_file"]
    assert client.events[0].duration == 0
