from __future__ import annotations

import json
from datetime import datetime, timezone

from aw_watcher_agent import cc_tailer, core
from aw_watcher_agent.client import AWClient


def _row(ts: str, *, sid: str = "s1", cwd: str = "/work/proj", **extra) -> str:
    rec = {"type": "user", "timestamp": ts, "sessionId": sid, "cwd": cwd}
    rec.update(extra)
    return json.dumps(rec)


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_parse_splits_on_idle_gap_and_keeps_metadata_only():
    lines = [
        _row("2026-11-10T09:00:00Z", message={"content": "secret prompt"}),
        _row("2026-11-10T09:10:00Z"),
        # 40 min idle -> new block
        _row("2026-11-10T09:50:00Z", cwd="/work/other"),
        _row("2026-11-10T09:55:00Z", cwd="/work/other"),
    ]
    blocks = cc_tailer.parse_session(lines)
    assert [(b.start, b.end) for b in blocks] == [
        (_dt("2026-11-10T09:00:00Z"), _dt("2026-11-10T09:10:00Z")),
        (_dt("2026-11-10T09:50:00Z"), _dt("2026-11-10T09:55:00Z")),
    ]
    event = blocks[0].to_event()
    assert event.data == {
        "harness": "claude-code",
        "session_id": "s1",
        "workspace": "/work/proj",
    }
    assert event.duration == 600
    assert "secret" not in json.dumps(event.to_payload())
    assert blocks[1].workspace == "/work/other"


def test_parse_ignores_sidechain_noise_and_malformed_rows():
    lines = [
        _row("2026-11-10T09:00:00Z"),
        _row("2026-11-10T11:00:00Z", isSidechain=True),
        json.dumps({"type": "attachment", "timestamp": "2026-11-10T12:00:00Z", "sessionId": "s1"}),
        "{not json",
        "",
        json.dumps({"type": "assistant", "timestamp": "bad", "sessionId": "s1"}),
        _row("2026-11-10T09:05:00Z"),
    ]
    blocks = cc_tailer.parse_session(lines)
    assert len(blocks) == 1
    assert blocks[0].end == _dt("2026-11-10T09:05:00Z")


class _Client(AWClient):
    def __init__(self):
        super().__init__("http://test")
        self.posted: list = []
        self.ensured: list = []

    def ensure_bucket(self, bid, etype, client_name, host):
        self.ensured.append((bid, etype))
        return True

    def post_event(self, bucket_id, event):
        self.posted.append((bucket_id, event))
        return len(self.posted)


def test_emit_holds_open_block_then_emits_each_block_once(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _row("2026-11-10T09:00:00Z"),
                _row("2026-11-10T09:10:00Z"),
                _row("2026-11-10T10:00:00Z"),
            ]
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 11, 10, 10, 5, tzinfo=timezone.utc)

    client = _Client()
    # Second block ended 5 min ago: still open, held back.
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 1
    assert client.ensured == [(core.bucket_id("host"), core.BUCKET_TYPE)]
    assert client.posted[0][1].duration == 600

    # Re-run at the same time: nothing new.
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 0

    # Later, the second block is finished and emitted exactly once.
    later = datetime(2026, 11, 10, 11, 0, tzinfo=timezone.utc)
    assert cc_tailer.emit_file(client, "host", transcript, now=later) == 1
    assert cc_tailer.emit_file(client, "host", transcript, now=later) == 0
    assert len(client.posted) == 2


def test_transcripts_excludes_nested_subagent_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    proj = tmp_path / "projects" / "-work-proj"
    (proj / "s1" / "subagents").mkdir(parents=True)
    (proj / "s1.jsonl").write_text("", encoding="utf-8")
    (proj / "s1" / "subagents" / "agent-x.jsonl").write_text("", encoding="utf-8")
    assert cc_tailer.transcripts() == [proj / "s1.jsonl"]
