from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

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


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_emit_resumes_after_last_successful_post(tmp_path, monkeypatch, fail_at):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        "\n".join(_row(f"2026-11-10T{hour:02d}:00:00Z") for hour in range(9, 13)),
        encoding="utf-8",
    )
    now = _dt("2026-11-10T13:00:00Z")
    client = _Client()

    # Start with a previously emitted block, then fail partway through the backlog.
    assert cc_tailer.emit_file(client, "host", transcript, now=_dt("2026-11-10T09:30:00Z")) == 1
    post_event = client.post_event
    attempts = 0

    def failing_post(bucket_id, event):
        nonlocal attempts
        attempts += 1
        if attempts == fail_at:
            raise OSError("temporary server failure")
        return post_event(bucket_id, event)

    monkeypatch.setattr(client, "post_event", failing_post)
    with pytest.raises(OSError, match="temporary server failure"):
        cc_tailer.emit_file(client, "host", transcript, now=now)

    # Use a fresh client as a timer restart would: accepted blocks must not replay.
    retry_client = _Client()
    assert cc_tailer.emit_file(retry_client, "host", transcript, now=now) == 4 - fail_at
    posted = client.posted + retry_client.posted
    assert [event.timestamp for _, event in posted] == [
        _dt(f"2026-11-10T{hour:02d}:00:00Z").isoformat() for hour in range(9, 13)
    ]
    assert cc_tailer.emit_file(retry_client, "host", transcript, now=now) == 0


def test_overlapping_emit_does_not_replay_inflight_block(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(_row("2026-11-10T09:00:00Z"), encoding="utf-8")
    now = _dt("2026-11-10T10:00:00Z")
    first = _Client()
    overlapping = _Client()
    post_event = first.post_event

    def post_with_overlap(bucket_id, event):
        # Another invocation arrives after the cursor read, before checkpointing.
        assert cc_tailer.emit_file(overlapping, "host", transcript, now=now) == 0
        return post_event(bucket_id, event)

    monkeypatch.setattr(first, "post_event", post_with_overlap)
    assert cc_tailer.emit_file(first, "host", transcript, now=now) == 1
    assert len(first.posted) == 1
    assert overlapping.posted == []
    # The lock must be released after finishing, including on exception paths.
    assert cc_tailer.emit_file(overlapping, "host", transcript, now=now) == 0


def test_transcripts_excludes_nested_subagent_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    proj = tmp_path / "projects" / "-work-proj"
    (proj / "s1" / "subagents").mkdir(parents=True)
    (proj / "s1.jsonl").write_text("", encoding="utf-8")
    (proj / "s1" / "subagents" / "agent-x.jsonl").write_text("", encoding="utf-8")
    assert cc_tailer.transcripts() == [proj / "s1.jsonl"]


def test_long_transcript_path_has_bounded_collision_resistant_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    project = tmp_path / ("p" * 180)
    project.mkdir()
    transcript = project / "12345678-1234-1234-1234-123456789abc.jsonl"
    transcript.write_text(_row("2026-11-10T09:00:00Z"), encoding="utf-8")
    client = _Client()
    now = _dt("2026-11-10T10:00:00Z")
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 1
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 0
    assert len(cc_tailer._cursor_path(transcript).name.encode()) < 100
    assert cc_tailer._cursor_path(tmp_path / "a_b" / "c.jsonl") != cc_tailer._cursor_path(
        tmp_path / "a" / "b_c.jsonl"
    )


def test_changed_gap_is_rejected_before_posting(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(
        "\n".join(_row(f"2026-11-10T09:{minute:02d}:00Z") for minute in (0, 12, 24)),
        encoding="utf-8",
    )
    client = _Client()
    now = _dt("2026-11-10T10:00:00Z")
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 1
    with pytest.raises(ValueError, match="gap"):
        cc_tailer.emit_file(client, "host", transcript, now=now, gap_s=600)
    assert len(client.posted) == 1
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 0


def test_interrupted_checkpoint_preserves_previous_cursor(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    cc_tailer._write_emitted(transcript, 1)
    cursor = cc_tailer._cursor_path(transcript)
    previous = cursor.read_bytes()
    write_text = Path.write_text

    def interrupted_write(path, *args, **kwargs):
        write_text(path, "{", encoding="utf-8")
        raise OSError("disk write interrupted")

    monkeypatch.setattr(Path, "write_text", interrupted_write)
    with pytest.raises(OSError, match="disk write interrupted"):
        cc_tailer._write_emitted(transcript, 2)
    assert cursor.read_bytes() == previous
    assert cc_tailer._read_emitted(transcript) == 1


@pytest.mark.parametrize("change", ["server", "hostname"])
def test_checkpoint_is_scoped_to_destination(tmp_path, monkeypatch, change):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(_row("2026-11-10T09:00:00Z"), encoding="utf-8")
    now = _dt("2026-11-10T10:00:00Z")
    first = _Client()
    second = _Client()
    host = "other" if change == "hostname" else "host"
    if change == "server":
        second.server = "http://other"
    assert cc_tailer.emit_file(first, "host", transcript, now=now) == 1
    assert cc_tailer.emit_file(second, host, transcript, now=now) == 1
    assert cc_tailer.emit_file(first, "host", transcript, now=now) == 0
    assert cc_tailer.emit_file(second, host, transcript, now=now) == 0
    assert len(first.posted) == len(second.posted) == 1


def test_incomplete_final_row_holds_trailing_block_until_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    prefix = _row("2026-11-10T08:00:00Z") + "\n" + _row("2026-11-10T09:00:00Z") + "\n"
    last = _row("2026-11-10T09:10:00Z")
    transcript.write_text(prefix + last[:20], encoding="utf-8")
    client = _Client()
    now = _dt("2026-11-10T10:00:00Z")
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 1
    assert client.posted[0][1].timestamp == _dt("2026-11-10T08:00:00Z").isoformat()
    transcript.write_text(prefix + last + "\n", encoding="utf-8")
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 1
    assert client.posted[-1][1].duration == 600
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 0


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("stage", ["connect", "execute"])
def test_sqlite_errors_obey_cli_strict_flag(tmp_path, monkeypatch, capsys, strict, stage):
    import sqlite3

    from aw_watcher_agent import cli

    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(_row("2026-11-10T09:00:00Z"), encoding="utf-8")
    closed = []

    class BrokenLock:
        def execute(self, sql):
            raise sqlite3.DatabaseError("broken lock")

        def close(self):
            closed.append(True)

    def connect(*args, **kwargs):
        if stage == "connect":
            raise sqlite3.OperationalError("cannot open lock")
        return BrokenLock()

    monkeypatch.setattr(cc_tailer.sqlite3, "connect", connect)
    argv = (["--strict"] if strict else []) + ["tail-claude-code", "--file", str(transcript)]
    assert cli.main(argv) == int(strict)
    assert "aw-watcher-agent:" in capsys.readouterr().err
    assert closed == ([True] if stage == "execute" else [])


@pytest.mark.parametrize("failure", [PermissionError("unreadable cursor"), OSError("I/O failure")])
def test_cursor_read_failure_never_replays_history(tmp_path, monkeypatch, failure):
    from pathlib import Path

    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(_row("2026-11-10T09:00:00Z"), encoding="utf-8")
    client = _Client()
    now = _dt("2026-11-10T10:00:00Z")
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 1
    cursor = cc_tailer._cursor_path(transcript, client.server, "host")
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == cursor:
            raise failure
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(OSError):
        cc_tailer.emit_file(client, "host", transcript, now=now)
    assert len(client.posted) == 1


@pytest.mark.parametrize(
    "contents", ["{", "{}", "[]", '{"emitted_blocks": true}', '{"emitted_blocks": -1}']
)
def test_invalid_cursor_never_initializes_progress(tmp_path, monkeypatch, contents):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    transcript.write_text(_row("2026-11-10T09:00:00Z"), encoding="utf-8")
    client = _Client()
    cursor = cc_tailer._cursor_path(transcript, client.server, "host")
    cursor.parent.mkdir()
    cursor.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        cc_tailer.emit_file(client, "host", transcript, now=_dt("2026-11-10T12:00:00Z"))
    assert client.posted == []


@pytest.mark.parametrize("change", ["prepend", "extend", "truncate", "workspace"])
def test_changed_checkpointed_prefix_rejected_before_posting(tmp_path, monkeypatch, change):
    monkeypatch.setattr(core, "state_dir", lambda: tmp_path / "state")
    transcript = tmp_path / "s1.jsonl"
    original = _row("2026-11-10T09:00:00Z")
    transcript.write_text(original + "\n", encoding="utf-8")
    client = _Client()
    now = _dt("2026-11-10T12:00:00Z")
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 1
    if change == "prepend":
        changed = (
            original + "\n" + _row("2026-11-10T08:00:00Z") + "\n" + _row("2026-11-10T11:00:00Z")
        )
    elif change == "extend":
        changed = original + "\n" + _row("2026-11-10T09:10:00Z")
    elif change == "workspace":
        changed = _row("2026-11-10T09:00:00Z", cwd="/different")
    else:
        changed = ""
    transcript.write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="checkpointed prefix"):
        cc_tailer.emit_file(client, "host", transcript, now=now)
    assert len(client.posted) == 1
    transcript.write_text(original + "\n" + _row("2026-11-10T11:00:00Z"), encoding="utf-8")
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 1
    assert cc_tailer.emit_file(client, "host", transcript, now=now) == 0


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("error", [OSError("unreadable transcript"), ValueError("invalid cursor")])
def test_scan_continues_after_transcript_failure(tmp_path, monkeypatch, capsys, strict, error):
    from aw_watcher_agent import cli

    first = tmp_path / "bad.jsonl"
    second = tmp_path / "good.jsonl"
    monkeypatch.setattr(cc_tailer, "transcripts", lambda: [first, second])
    visited = []

    def emit(client, host, path, **kwargs):
        visited.append(path)
        if path == first:
            raise error
        return 2

    monkeypatch.setattr(cc_tailer, "emit_file", emit)
    argv = (["--strict"] if strict else []) + ["tail-claude-code"]
    assert cli.main(argv) == int(strict)
    assert visited == [first, second]
    output = capsys.readouterr()
    assert str(first) in output.err
    assert str(error) in output.err
    assert "events=2" in output.out
