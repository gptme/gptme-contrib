"""Regression tests from the 2026-11-24 verified bug hunt (follower data loss/duplicates)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gptme_agent_events import Follower
from gptme_agent_events.event import AgentEvent, normalize_ts
from gptme_agent_events.follow import claude_code_source, gptme_source
from gptme_agent_events.gitwatch import GitWatcher

NOW = 1_794_484_800.0  # 2026-11-12T12:00:00Z

ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


def touch(path: Path, clock: Clock) -> None:
    os.utime(path, (clock.t, clock.t))


def cc_tool(call_id: str, ts: str, name: str = "Read", command: str | None = None) -> str:
    block: dict = {"type": "tool_use", "id": call_id, "name": name}
    if command is not None:
        block["input"] = {"command": command}
    rec = {
        "type": "assistant",
        "sessionId": "sess-1",
        "timestamp": ts,
        "message": {"id": "m-" + call_id, "content": [block]},
    }
    return json.dumps(rec) + "\n"


def cc_result(call_id: str, ts: str, text: str, is_error: bool = False) -> str:
    rec = {
        "type": "user",
        "sessionId": "sess-1",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": text,
                    "is_error": is_error,
                }
            ]
        },
    }
    return json.dumps(rec) + "\n"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, msg: str, session: str | None = None) -> str:
    (repo / "f.txt").write_text(msg)
    git("add", "f.txt", cwd=repo)
    body = msg + (f"\n\nGit-Session-Id: {session}" if session else "")
    git("commit", "-q", "-m", body, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git("init", "-q", "-b", "main", cwd=r)
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=r)
    return r.resolve()


# -- R1-1 ---------------------------------------------------------------------


def test_r1_1_startup_file_rewritten_before_any_event_replays_history(tmp_path):
    """A file followed from its end at startup must not replay its history when
    gptme rewrites it (compaction via temp file) before anything new was reported."""
    clock = Clock()
    root = tmp_path / "logs"
    convo = root / "2026-11-12-convo"
    convo.mkdir(parents=True)
    log = convo / "conversation.jsonl"

    def msg(text: str, ts: str) -> str:
        return json.dumps({"role": "assistant", "content": text, "timestamp": ts}) + "\n"

    history = msg("old one", "2026-11-12T09:00:01") + msg("old two", "2026-11-12T09:30:00")
    log.write_text(history)
    touch(log, clock)
    f = Follower([gptme_source(root)], rescan_interval=0, clock=clock)
    assert f.poll() == []  # history is not replayed at startup
    clock.t += 60  # the new message is written after the follower started
    new_ts = iso(clock.t - 30)
    tmp = convo / "tmp"
    tmp.write_text(history + msg("new", new_ts))
    tmp.replace(log)
    touch(log, clock)
    assert [e.ts for e in f.poll()] == [normalize_ts(new_ts)]


# -- R1-2 ---------------------------------------------------------------------


def test_r1_2_long_running_call_result_lost_after_file_retired(tmp_path):
    """A tool call that runs longer than active_window retires the file; when its
    result arrives, the re-followed file has a fresh extractor and the result
    (here a successful `gh pr create`) is silently dropped."""
    clock = Clock()
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    t = proj / "sess-1.jsonl"
    t.write_text("")
    touch(t, clock)
    f = Follower(
        [claude_code_source(tmp_path / "projects")],
        rescan_interval=0,
        full_scan_interval=60,
        clock=clock,
    )
    f.poll()
    with t.open("a") as fh:
        fh.write(cc_tool("c1", iso(clock.t), "Bash", "make test && gh pr create --fill"))
    touch(t, clock)
    assert [e.kind for e in f.poll()] == ["tool"]
    clock.t += 601  # the command is still running; transcript is silent
    assert f.poll() == []
    with t.open("a") as fh:
        fh.write(cc_result("c1", iso(clock.t), "https://github.com/o/r/pull/7"))
    touch(t, clock)
    events = []
    for _ in range(3):
        clock.t += 61
        events += f.poll()
    assert [e.kind for e in events] == ["pr_open"]


# -- R1-3 ---------------------------------------------------------------------


def test_r1_3_new_session_in_cold_project_dir_loses_first_minutes(tmp_path):
    """With default intervals, a new session in a project dir idle >24h is only
    discovered by the full sweep (300s), but events older than discovery-120s are
    dropped, so the session's first ~3 minutes vanish."""
    clock = Clock()
    root = tmp_path / "projects"
    cold = root / "old-project"
    cold.mkdir(parents=True)
    (cold / "ancient.jsonl").write_text("")
    os.utime(cold / "ancient.jsonl", (NOW - 3 * 86400, NOW - 3 * 86400))
    os.utime(cold, (NOW - 3 * 86400, NOW - 3 * 86400))
    f = Follower([claude_code_source(root)], clock=clock)  # default intervals
    assert f.poll() == []
    t = cold / "sess-1.jsonl"
    events = []
    for i, offset in enumerate((10, 100, 170, 250)):
        while clock.t < NOW + offset:
            clock.t += 5
            events += f.poll()
        with t.open("a") as fh:
            fh.write(cc_tool(f"c{i}", iso(clock.t)))
        touch(t, clock)
    while clock.t < NOW + 330:
        clock.t += 5
        events += f.poll()
    assert [e.kind for e in events].count("tool") == 4


# -- R1-5 ---------------------------------------------------------------------


def test_r1_5_commit_double_reported_when_tool_result_lags_reflog(repo):
    """`git commit && git push` (or a slow post-commit/pre-push hook) writes the tool
    result well after the reflog entry. The git event is flushed after 5s, then the
    shell event, carrying the very same sha, is flushed too: two commit chimes."""
    now = [100.0]
    w = GitWatcher()
    w.watch(str(repo))
    f = Follower([], git=w, clock=lambda: now[0])
    sha = commit(repo, "then push", None)
    events = f._with_git([], False)
    now[0] += 6
    events += f._with_git([], False)  # git commit flushed, unmatched
    now[0] += 20  # push / pre-push hook finishes, tool result arrives
    shell = AgentEvent(
        kind="commit",
        ts="2099-01-01T00:00:00+00:00",
        harness="claude-code",
        session="s",
        tool="Bash",
        detail={"dir": str(repo), "sha": sha[:7]},
    )
    events += f._with_git([shell], False)
    now[0] += 6
    events += f._with_git([], False)
    commits = [e for e in events if e.kind == "commit"]
    assert len(commits) == 1, [
        (e.session, e.detail.get("source"), e.detail.get("sha")) for e in commits
    ]


def test_r1_5b_shell_commit_flushed_without_reflog_not_repeated_when_reflog_lands(repo):
    """A shell commit released after 5s with no reflog (core.logAllRefUpdates=false
    or a late reflog) must count as reported: when the reflog entry for that same
    sha shows up later, it is not a second landing."""
    now = [100.0]
    w = GitWatcher()
    w.watch(str(repo))
    f = Follower([], git=w, clock=lambda: now[0])
    sha = commit(repo, "late reflog", None)
    log = repo / ".git" / "logs" / "HEAD"
    saved = log.read_bytes()
    log.write_bytes(b"")  # the reflog entry is not visible yet
    shell = AgentEvent(
        kind="commit",
        ts="2099-01-01T00:00:00+00:00",
        harness="claude-code",
        session="s",
        tool="Bash",
        detail={"dir": str(repo), "sha": sha[:7]},
    )
    events = f._with_git([shell], False)
    now[0] += 6
    events += f._with_git([], False)  # released from shell evidence alone
    assert [e.session for e in events] == ["s"]
    log.write_bytes(saved)
    now[0] += 20
    events += f._with_git([], False)
    now[0] += 6
    events += f._with_git([], False)
    commits = [e for e in events if e.kind == "commit"]
    assert len(commits) == 1, [
        (e.session, e.detail.get("source"), e.detail.get("sha")) for e in commits
    ]


def test_r1_2b_retired_gptme_log_rewritten_with_result_keeps_the_landing(tmp_path):
    """gptme only replaces conversation.jsonl (edit/undo/compaction) by rewriting the
    whole log, so the replacement still holds the open call: a fresh extractor that
    re-reads it from the start pairs the result. History stays unreported."""
    clock = Clock()
    root = tmp_path / "logs"
    convo = root / "2026-11-12-convo"
    convo.mkdir(parents=True)
    log = convo / "conversation.jsonl"
    log.write_text("")
    touch(log, clock)
    f = Follower([gptme_source(root)], rescan_interval=0, full_scan_interval=60, clock=clock)
    f.poll()

    def msg(role: str, content: str, ts: str, call_id: str | None = None) -> str:
        rec = {"role": role, "content": content, "timestamp": ts}
        if call_id:
            rec["call_id"] = call_id
        return json.dumps(rec) + "\n"

    call = msg("assistant", '\n@gh(call_1): {"command": "pr create --fill"}', iso(clock.t))
    log.write_text(call)
    touch(log, clock)
    assert [e.kind for e in f.poll()] == ["tool"]
    clock.t += 601  # the call is still running; the file is retired
    assert f.poll() == []
    tmp = convo / "tmp"
    tmp.write_text(call + msg("system", "https://github.com/o/r/pull/7", iso(clock.t), "call_1"))
    tmp.replace(log)  # new inode
    touch(log, clock)
    events = []
    for _ in range(3):
        clock.t += 61
        events += f.poll()
    assert [e.kind for e in events] == ["pr_open"]


def test_r1_5c_reflog_commit_not_repeated_when_tool_result_lags_past_two_minutes(repo):
    """Review finding on gptme/gptme-contrib#2371: a reflog commit flushed unmatched
    stays reported when the shell evidence arrives minutes later (a slow push or a
    pre-push hook running the full suite), not only within 120s."""
    now = [100.0]
    w = GitWatcher()
    w.watch(str(repo))
    f = Follower([], git=w, clock=lambda: now[0])
    sha = commit(repo, "slow hook", None)
    events = f._with_git([], False)
    now[0] += 6
    events += f._with_git([], False)  # released from the reflog alone
    now[0] += 150
    shell = AgentEvent(
        kind="commit",
        ts="2099-01-01T00:00:00+00:00",
        harness="claude-code",
        session="s",
        tool="Bash",
        detail={"dir": str(repo), "sha": sha[:7]},
    )
    events += f._with_git([shell], False)
    now[0] += 6
    events += f._with_git([], False)
    commits = [e for e in events if e.kind == "commit"]
    assert len(commits) == 1, [
        (e.session, e.detail.get("source"), e.detail.get("sha")) for e in commits
    ]
