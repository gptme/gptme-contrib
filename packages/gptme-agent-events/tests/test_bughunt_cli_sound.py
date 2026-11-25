"""Regression tests from the 2026-11-24 verified bug hunt (cli + sound)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from click.testing import CliRunner

from gptme_agent_events.cli import main
from gptme_agent_events.sound import DEFAULT_SOUNDS

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def _sounds_dir(tmp_path: Path) -> Path:
    d = tmp_path / "media"
    d.mkdir()
    for name in set(DEFAULT_SOUNDS.values()):
        (d / f"{name}.wav").write_bytes(b"RIFF")
    return d


def _event_line(kind: str = "turn_end", agent: str = "bob") -> str:
    return (
        json.dumps(
            {
                "kind": kind,
                "ts": "2026-11-24T10:00:00.000+00:00",
                "agent": agent,
                "harness": "claude-code",
                "session": "sess1234",
            }
        )
        + "\n"
    )


class _FakeProc:
    def poll(self):
        return 0


# --------------------------------------------------------------------------
# R3-1: `tail --once` silently drops every commit/push when git watching is on
# --------------------------------------------------------------------------


def _cc_commit_trajectory(projects: Path, repo: Path, sha: str) -> None:
    proj = projects / "p"
    proj.mkdir(parents=True)
    rows = [
        {
            "type": "assistant",
            "sessionId": "sess-cc",
            "cwd": str(repo),
            "timestamp": "2099-01-01T00:00:00.000Z",
            "message": {
                "id": "m1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "c1",
                        "name": "Bash",
                        "input": {"command": "git commit -m 'via agent'"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": "sess-cc",
            "cwd": str(repo),
            "timestamp": "2099-01-01T00:00:01.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c1",
                        "content": f"[main {sha[:7]}] via agent",
                    }
                ]
            },
        },
    ]
    (proj / "sess-cc.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_r3_1_tail_once_from_start_keeps_commits_with_git_watching(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", "f.txt", cwd=repo)
    # The session committed 5 minutes ago: still inside tail's 600 s active
    # window (so --from-start replays it) but older than GitWatcher's 120 s
    # registration slack, so the reflog line is (correctly) not re-reported.
    env = {**GIT_ENV, "GIT_COMMITTER_DATE": f"{int(time.time()) - 300} +0000"}
    subprocess.run(["git", "commit", "-q", "-m", "via agent"], cwd=repo, env=env, check=True)
    sha = _git("rev-parse", "HEAD", cwd=repo)
    repo = repo.resolve()
    claude_dir = tmp_path / "claude"
    _cc_commit_trajectory(claude_dir / "projects", repo, sha)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    args = ["tail", "--agent", "bob", "--harness", "claude-code", "--from-start", "--once"]
    runner = CliRunner()
    no_git = runner.invoke(main, [*args, "--no-git"])
    with_git = runner.invoke(main, args)
    kinds_no_git = [json.loads(line)["kind"] for line in no_git.output.splitlines()]
    kinds_git = [json.loads(line)["kind"] for line in with_git.output.splitlines()]

    # Sanity: the extractor does see the commit.
    assert "commit" in kinds_no_git
    # Defect: the default (git-watching) replay holds the commit back for a
    # later poll that --once never runs, so it is never printed.
    assert "commit" in kinds_git, f"--once dropped the commit: {kinds_git}"


# --------------------------------------------------------------------------
# R3-2: --agent-volume accepts nan/inf/negative/>1, nan/inf crash the sink
# --------------------------------------------------------------------------


def test_r3_2_non_finite_agent_volume_does_not_crash_sound_loop(tmp_path, monkeypatch):
    media = _sounds_dir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/ffplay" if cmd == "ffplay" else None)
    monkeypatch.setattr(
        "gptme_agent_events.sound.SoundPlayer._popen", staticmethod(lambda cmd: _FakeProc())
    )
    result = CliRunner().invoke(
        main,
        ["sound", "--sounds-dir", str(media), "--agent-volume", "bob=nan", "-q"],
        input=_event_line(agent="bob") + _event_line(agent="alice"),
    )
    # Either reject the option up front (usage error, exit 2) or survive the
    # event; an uncaught ValueError traceback mid-stream is the defect.
    assert not isinstance(result.exception, (ValueError, OverflowError)), repr(result.exception)


def test_r3_2b_agent_volume_cannot_exceed_volume_ceiling(tmp_path, monkeypatch):
    media = _sounds_dir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/afplay" if cmd == "afplay" else None)
    spawned: list[list[str]] = []

    def fake_popen(cmd):
        spawned.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("gptme_agent_events.sound.SoundPlayer._popen", staticmethod(fake_popen))
    result = CliRunner().invoke(
        main,
        ["sound", "--sounds-dir", str(media), "--volume", "0.5", "--agent-volume", "bob=-3", "-q"],
        input=_event_line(agent="bob"),
    )
    if result.exit_code == 2:  # rejected as a usage error: fixed
        return
    # A crash before the sink runs also leaves `spawned` empty, so an
    # `all()` over it would pass vacuously. Require a successful run first.
    assert result.exit_code == 0, f"sound run failed: {result.output!r}"
    assert spawned, "no player invocation: cannot verify the volume ceiling"
    played = [float(cmd[2]) for cmd in spawned]
    assert all(0.0 <= v <= 1.0 for v in played), f"player volumes out of [0,1]: {spawned}"


# --------------------------------------------------------------------------
# R3-3: a player spawn failure (OSError) kills the whole sound loop
# --------------------------------------------------------------------------


def test_r3_3_player_spawn_oserror_does_not_kill_sound_loop(tmp_path, monkeypatch):
    media = _sounds_dir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/paplay" if cmd == "paplay" else None)

    def failing_popen(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr("gptme_agent_events.sound.subprocess.Popen", failing_popen)
    result = CliRunner().invoke(
        main,
        ["sound", "--sounds-dir", str(media)],
        input=_event_line("turn_end") + _event_line("commit"),
    )
    assert result.exception is None, f"sound loop died: {result.exception!r}"
    assert len(result.output.splitlines()) == 2, result.output


# --------------------------------------------------------------------------
# R3-4: tail exits 120 with a BrokenPipeError message when the consumer leaves
# --------------------------------------------------------------------------


def test_r3_4_tail_exits_cleanly_when_consumer_closes_pipe(tmp_path):
    claude_dir = tmp_path / "claude"
    proj = claude_dir / "projects" / "p"
    proj.mkdir(parents=True)
    rows = [
        {
            "type": "assistant",
            "sessionId": "s",
            "timestamp": "2026-11-24T10:00:00.000Z",
            "message": {
                "id": f"m{i}",
                "content": [{"type": "tool_use", "id": f"c{i}", "name": "Read", "input": {}}],
            },
        }
        for i in range(2000)
    ]
    (proj / "s.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_dir)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gptme_agent_events.cli",
            "tail",
            "--harness",
            "claude-code",
            "--from-start",
            "--once",
            "--no-git",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.read(10)
    proc.stdout.close()  # the consumer (e.g. `sound` over ssh, or `head`) goes away
    err = proc.stderr.read().decode()
    code = proc.wait(timeout=60)
    # The code explicitly catches BrokenPipeError to exit quietly, but the
    # interpreter's final stdout flush still raises.
    assert (code, "BrokenPipeError" in err) == (0, False), (code, err)
