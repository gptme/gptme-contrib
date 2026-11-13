"""The follower discovers trajectories, tails them, and survives rewrites."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gptme_agent_events import Follower
from gptme_agent_events.event import normalize_ts
from gptme_agent_events.follow import claude_code_source, gptme_source


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def cc_line(call_id: str, name: str = "Read", ts: str = "2026-11-12T10:00:00.000Z") -> str:
    rec = {
        "type": "assistant",
        "sessionId": "sess-1",
        "timestamp": ts,
        "message": {"id": call_id, "content": [{"type": "tool_use", "id": call_id, "name": name}]},
    }
    return json.dumps(rec) + "\n"


def touch(path: Path, clock: Clock) -> None:
    os.utime(path, (clock.t, clock.t))


def make(tmp_path: Path, clock: Clock, **kw) -> Follower:
    return Follower(
        [claude_code_source(tmp_path / "projects")],
        agent="bob",
        rescan_interval=0,
        full_scan_interval=60,
        clock=clock,
        **kw,
    )


def test_existing_files_followed_from_end(tmp_path):
    clock = Clock()
    proj = tmp_path / "projects" / "-home-bob"
    proj.mkdir(parents=True)
    t = proj / "sess-1.jsonl"
    t.write_text(cc_line("old"))
    touch(t, clock)
    f = make(tmp_path, clock)
    assert f.poll() == []  # history is not replayed
    with t.open("a") as fh:
        fh.write(cc_line("new"))
    touch(t, clock)
    events = f.poll()
    assert [(e.kind, e.agent, e.session) for e in events] == [("tool", "bob", "sess-1")]


def test_from_start_replays_recent_files(tmp_path):
    clock = Clock()
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    t = proj / "s.jsonl"
    t.write_text(cc_line("a") + cc_line("b"))
    touch(t, clock)
    assert len(make(tmp_path, clock, from_start=True).poll()) == 2


def test_new_file_announces_session_start(tmp_path):
    clock = Clock()
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    f = make(tmp_path, clock)
    assert f.poll() == []
    t = proj / "sess-1.jsonl"
    t.write_text(cc_line("a"))
    touch(t, clock)
    assert [e.kind for e in f.poll()] == ["session_start", "tool"]
    assert f.poll() == []


def test_subagent_files_do_not_start_sessions(tmp_path):
    clock = Clock()
    sub = tmp_path / "projects" / "p" / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    f = make(tmp_path, clock)
    f.poll()
    t = sub / "agent-1.jsonl"
    t.write_text(cc_line("a"))
    touch(t, clock)
    events = f.poll()
    assert [e.kind for e in events] == ["tool"]
    assert events[0].subagent


def test_partial_lines_wait_for_newline(tmp_path):
    clock = Clock()
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    t = proj / "s.jsonl"
    t.write_text("")
    touch(t, clock)
    f = make(tmp_path, clock)
    f.poll()
    line = cc_line("a")
    with t.open("a") as fh:
        fh.write(line[:20])
    assert f.poll() == []
    with t.open("a") as fh:
        fh.write(line[20:])
    assert [e.kind for e in f.poll()] == ["tool"]


def test_rewritten_file_reports_only_newer_events(tmp_path):
    clock = Clock()
    root = tmp_path / "logs"
    convo = root / "2026-11-12-convo"
    convo.mkdir(parents=True)
    log = convo / "conversation.jsonl"

    def msg(text: str, ts: str) -> str:
        return json.dumps({"role": "assistant", "content": text, "timestamp": ts}) + "\n"

    log.write_text("")
    touch(log, clock)
    f = Follower([gptme_source(root)], rescan_interval=0, clock=clock)
    f.poll()
    with log.open("a") as fh:
        fh.write(msg("one", "2026-11-12T11:00:01"))
    assert [e.ts for e in f.poll()] == [normalize_ts("2026-11-12T11:00:01")]
    # gptme replaces the file through a temp file: new inode, same prefix.
    tmp = convo / "tmp"
    tmp.write_text(msg("one", "2026-11-12T11:00:01") + msg("two", "2026-11-12T11:00:02"))
    tmp.replace(log)
    touch(log, clock)
    assert [e.ts for e in f.poll()] == [normalize_ts("2026-11-12T11:00:02")]


def test_idle_files_skipped_until_active_again(tmp_path):
    clock = Clock()
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    t = proj / "s.jsonl"
    t.write_text("")
    os.utime(t, (clock.t - 10_000, clock.t - 10_000))
    f = make(tmp_path, clock, active_window=600)
    f.poll()
    with t.open("a") as fh:
        fh.write(cc_line("resumed"))
    touch(t, clock)  # a resumed session: mtime is fresh again
    assert f.poll() == []  # idle files are only re-checked by the full sweep
    clock.t += 61
    touch(t, clock)
    assert [e.kind for e in f.poll()] == ["tool"]


def test_missing_roots_are_fine(tmp_path):
    f = Follower([claude_code_source(tmp_path / "nope")], rescan_interval=0, clock=Clock())
    assert f.poll() == []


NOW = 1_794_484_800.0  # 2026-11-12T12:00:00Z


def test_history_moved_into_place_is_not_replayed(tmp_path):
    """A conversation copied in after startup must not chime for old commits."""
    clock = Clock()
    clock.t = NOW
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    f = make(tmp_path, clock)
    f.poll()
    t = proj / "copied.jsonl"
    t.write_text(cc_line("old", ts="2026-11-10T09:00:00.000Z"))
    touch(t, clock)
    assert f.poll() == []  # old history: no events, no session_start
    with t.open("a") as fh:
        fh.write(cc_line("new", ts="2026-11-12T12:00:01.000Z"))
    touch(t, clock)
    assert [e.kind for e in f.poll()] == ["session_start", "tool"]


def test_quick_scan_skips_cold_dirs_but_finds_new_ones(tmp_path):
    clock = Clock()
    clock.t = NOW
    root = tmp_path / "projects"
    cold, warm = root / "cold", root / "warm"
    cold.mkdir(parents=True)
    warm.mkdir()
    (cold / "old.jsonl").write_text("")
    os.utime(cold, (NOW - 3 * 86400, NOW - 3 * 86400))
    f = make(tmp_path, clock)
    f.poll()
    assert cold not in f._warm and warm in f._warm
    # A new file in a cold dir waits for the full sweep...
    (cold / "s1.jsonl").write_text(cc_line("a", ts="2026-11-12T12:00:01.000Z"))
    os.utime(cold, (NOW - 3 * 86400, NOW - 3 * 86400))
    assert f.poll() == []
    # ...but a brand-new project dir is seen by the next quick scan.
    fresh = root / "fresh"
    fresh.mkdir()
    (fresh / "s2.jsonl").write_text(cc_line("b", ts="2026-11-12T12:00:02.000Z"))
    assert [e.kind for e in f.poll()] == ["session_start", "tool"]
    clock.t += 61
    assert [e.session for e in f.poll() if e.kind == "tool"] == ["sess-1"]


def test_idle_files_are_retired_and_vanished_ones_forgotten(tmp_path):
    clock = Clock()
    clock.t = NOW
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    t = proj / "s.jsonl"
    t.write_text("")
    touch(t, clock)
    f = make(tmp_path, clock)
    f.poll()
    assert t in f._files
    clock.t += 601
    f.poll()
    assert t not in f._files and t in f._cold
    t.unlink()
    clock.t += 61
    f.poll()
    assert t not in f._cold
