"""The sound sink maps events to gptme sounds and throttles bursts."""

from __future__ import annotations

import json
import subprocess

from click.testing import CliRunner

from gptme_agent_events import AgentEvent
from gptme_agent_events.cli import main
from gptme_agent_events.sound import DEFAULT_SOUNDS, SoundPlayer, sound_key


class FakeProc:
    def __init__(self) -> None:
        self.done = False

    def poll(self):
        return 0 if self.done else None


def ev(kind: str, tool_class: str | None = None, agent: str = "bob") -> AgentEvent:
    return AgentEvent(
        kind=kind, ts="t", harness="claude-code", session="s", agent=agent, tool_class=tool_class
    )


def make(tmp_path, **kw):
    for name in set(DEFAULT_SOUNDS.values()):
        (tmp_path / f"{name}.wav").write_bytes(b"RIFF")
    spawned: list[list[str]] = []
    procs: list[FakeProc] = []

    def spawn(cmd):
        spawned.append(cmd)
        p = FakeProc()
        procs.append(p)
        return p

    clock = {"t": 0.0}
    player = SoundPlayer(tmp_path, spawn=spawn, clock=lambda: clock["t"], **kw)
    return player, spawned, procs, clock


def test_sound_keys():
    assert sound_key(ev("tool", "write")) == "tool:write"
    assert sound_key(ev("tool")) == "tool:other"
    assert sound_key(ev("commit")) == "commit"


def test_every_kind_and_class_has_a_default():
    from gptme_agent_events.event import KINDS, TOOL_CLASSES

    for kind in KINDS:
        if kind != "tool":
            assert kind in DEFAULT_SOUNDS
    for cls in TOOL_CLASSES:
        assert f"tool:{cls}" in DEFAULT_SOUNDS


def test_plays_mapped_sound(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/afplay" if cmd == "afplay" else None)
    player, spawned, _, _ = make(tmp_path, volume=0.5, agent_volume={"bob": 0.5})
    assert player.handle(ev("turn_end")) == "bell"
    assert spawned[0][:3] == ["afplay", "-v", "0.25"]
    assert spawned[0][3].endswith("bell.wav")


def test_tool_burst_is_throttled_but_milestones_are_not(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/" + cmd)
    player, spawned, _procs, clock = make(tmp_path, min_gap=0.5)
    assert player.handle(ev("tool", "shell"))
    assert player.handle(ev("tool", "shell")) is None  # within min_gap
    assert player.handle(ev("commit")) == "chime_clean_ring"
    clock["t"] = 1.0
    assert player.handle(ev("tool", "read")) == "page_turn"
    assert len(spawned) == 3


def test_concurrency_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/" + cmd)
    player, _spawned, procs, _clock = make(tmp_path, min_gap=0, max_concurrent=2)
    player.handle(ev("tool", "shell"))
    player.handle(ev("tool", "shell"))
    assert player.handle(ev("tool", "shell")) is None
    for p in procs:
        p.done = True
    assert player.handle(ev("tool", "shell"))


def test_override_can_silence(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/" + cmd)
    player, spawned, _, _ = make(tmp_path, mapping={"tool:shell": ""})
    assert player.handle(ev("tool", "shell")) is None
    assert spawned == []


def test_cli_dry_run_reads_stdin(tmp_path):
    lines = "\n".join(
        [
            ev("turn_end").to_json(),
            "not json",
            json.dumps({"kind": "commit", "agent": "alice", "future_field": 1}),
            ev("tool", "write", agent="gordon").to_json(),
        ]
    )
    result = CliRunner().invoke(
        main, ["sound", "--dry-run", "--only-agent", "bob", "--only-agent", "alice"], input=lines
    )
    assert result.exit_code == 0, result.output
    assert "bob turn_end -> bell" in result.output
    assert "alice commit -> chime_clean_ring" in result.output
    assert "gordon" not in result.output


def test_cli_list():
    result = CliRunner().invoke(main, ["sound", "--list", "--map", "commit="])
    assert result.exit_code == 0
    assert "commit           (silent)" in result.output


def test_popen_used_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/" + cmd)
    calls: list[list[str]] = []

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    (tmp_path / "bell.wav").write_bytes(b"RIFF")
    SoundPlayer(tmp_path).handle(ev("turn_end"))
    assert calls and calls[0][0] == "afplay"
