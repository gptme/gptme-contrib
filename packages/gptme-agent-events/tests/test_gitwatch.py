"""Commits and pushes come from git's reflogs, attributed by Git-Session-Id."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from gptme_agent_events import Follower
from gptme_agent_events.extractors import _github_slug
from gptme_agent_events.follow import claude_code_source
from gptme_agent_events.gitwatch import GitWatcher

ENV = {
    **os.environ,
    # Fixtures must not inherit signing or session-stamping hooks from the host.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


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
    git("remote", "add", "origin", "git@github.com:gptme/gptme.git", cwd=r)
    return r.resolve()


def test_commits_attributed_by_trailer_and_history_not_replayed(repo):
    commit(repo, "old", "sess-old")
    w = GitWatcher("bob", slack=0, clock=lambda: 1e10)  # registered "now": old is history
    assert w.watch(str(repo)) is not None
    assert w.poll() == []
    w.slack = 1e11  # from here on, anything new counts
    sha = commit(repo, "new", "sess-1")
    (ev,) = w.poll()
    assert (ev.kind, ev.session, ev.agent, ev.harness) == ("commit", "sess-1", "bob", "git")
    assert ev.detail == {
        "sha": sha[:12],
        "source": "git",
        "repo": "gptme/gptme",
        "dir": str(repo / ".git"),
    }
    assert ev.ts.endswith("+00:00")


def test_recent_commit_before_registration_is_kept(repo):
    """A commit written just before the repo was registered (the usual race) counts."""
    commit(repo, "just now", "sess-2")
    w = GitWatcher(slack=120)
    w.watch(str(repo))
    assert [e.session for e in w.poll()] == ["sess-2"]
    assert w.poll() == []


def test_reset_and_checkout_are_not_commits(repo):
    commit(repo, "a", "s")
    commit(repo, "b", "s")
    w = GitWatcher(slack=120)
    w.watch(str(repo))
    w.poll()
    git("reset", "-q", "--hard", "HEAD~1", cwd=repo)
    git("checkout", "-q", "-b", "other", cwd=repo)
    assert w.poll() == []


def test_push_attributed_through_pushed_commit(repo, tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("remote", "set-url", "origin", str(remote), cwd=repo)
    git("config", "remote.origin.url", str(remote), cwd=repo)
    w = GitWatcher(slack=120)
    w.watch(str(repo))
    w.poll()
    commit(repo, "c", "sess-3")
    git("push", "-q", "origin", "main", cwd=repo)
    events = w.poll(rediscover=True)  # the remote-tracking reflog is new
    assert [(e.kind, e.session) for e in events] == [("commit", "sess-3"), ("push", "sess-3")]
    assert events[1].detail["ref"] == "origin/main"


def test_worktree_commits(repo, tmp_path):
    commit(repo, "base", "s")
    w = GitWatcher(slack=120)
    w.watch(str(repo))
    w.poll()
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", "-b", "feat", str(wt), cwd=repo)
    sha = commit(wt, "in worktree", "sess-wt")
    events = [e for e in w.poll(rediscover=True) if e.kind == "commit"]
    assert [(e.session, e.detail["sha"]) for e in events] == [("sess-wt", sha[:12])]
    assert w.watches(str(wt))  # same common dir


def test_non_repo_dirs_are_ignored(tmp_path):
    w = GitWatcher()
    assert w.watch(str(tmp_path)) is None
    assert w.watch("relative/path") is None
    assert w.poll() == []


@pytest.mark.parametrize("session", ["sess-cc", None])
def test_follower_reports_one_commit_from_git_with_session_harness(repo, tmp_path, session):
    """The shell-detected commit is replaced by the git one: no double chime."""
    _github_slug.cache_clear()
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    t = proj / "sess-cc.jsonl"
    t.write_text("")
    f = Follower(
        [claude_code_source(tmp_path / "projects")],
        agent="bob",
        rescan_interval=0,
        git=GitWatcher("bob", slack=120),
    )
    f.poll()
    sha = commit(repo, "via agent", session)
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
    with t.open("a") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))
    commits = [e for e in f.poll() if e.kind == "commit"]
    assert len(commits) == 1
    (c,) = commits
    assert (c.session, c.harness, c.detail["source"]) == ("sess-cc", "claude-code", "git")
    if session is None:
        assert c.tool == "Bash"


def test_repo_discovery_retries_after_git_init(tmp_path):
    w = GitWatcher()
    assert w.watch(str(tmp_path)) is None
    git("init", "-q", cwd=tmp_path)
    assert w.watch(str(tmp_path)) == tmp_path / ".git"


@pytest.mark.parametrize("rewrite", ["replace", "truncate"])
def test_reflog_rewrite_does_not_replay_but_keeps_new_same_second_commit(repo, rewrite):
    w = GitWatcher()
    w.watch(str(repo))
    commit(repo, "first", "s")
    assert len(w.poll()) == 1
    log = repo / ".git" / "logs" / "HEAD"
    old = log.read_bytes()
    # A new entry may share the second-resolution timestamp of the old entry.
    sha = commit(repo, "second", "s")
    new = log.read_bytes()[len(old) :]
    if rewrite == "replace":
        replacement = log.with_suffix(".replacement")
        replacement.write_bytes(old + new)
        replacement.replace(log)
    else:
        log.write_bytes(b"")
        assert w.poll() == []
        log.write_bytes(old + new)
    assert [e.detail["sha"] for e in w.poll()] == [sha[:12]]
    assert w.poll() == []


@pytest.mark.parametrize("shell_first", [True, False])
def test_follower_correlates_across_polls(repo, shell_first):
    from gptme_agent_events.event import AgentEvent

    now = [100.0]
    w = GitWatcher()
    w.watch(str(repo))
    f = Follower([], git=w, clock=lambda: now[0])
    sha = commit(repo, "delayed", None)
    shell = AgentEvent(
        kind="commit",
        ts="2099-01-01T00:00:00+00:00",
        harness="codex",
        session="s",
        tool="exec_command",
        detail={"dir": str(repo), "sha": sha[:7]},
    )
    if shell_first:
        # Simulate a reflog becoming visible on the next pass.
        log = repo / ".git" / "logs" / "HEAD"
        saved = log.read_bytes()
        log.write_bytes(b"")
        assert f._with_git([shell], False) == []
        log.write_bytes(saved)
        events = f._with_git([], False)
    else:
        assert f._with_git([], False) == []
        now[0] += 1
        events = f._with_git([shell], False)
    (event,) = events
    assert (event.session, event.harness, event.tool) == ("s", "codex", "exec_command")
    now[0] += 10
    assert f._with_git([], False) == []


def test_follower_falls_back_without_reflogs(repo):
    from gptme_agent_events.event import AgentEvent

    now = [100.0]
    f = Follower([], git=GitWatcher(), clock=lambda: now[0])
    shell = AgentEvent(
        kind="push",
        ts="2099-01-01T00:00:00+00:00",
        harness="gptme",
        session="s",
        tool="shell",
        detail={"dir": str(repo)},
    )
    assert f._with_git([shell], False) == []
    now[0] += 6
    assert f._with_git([], False) == [shell]
    assert f._with_git([], False) == []


def test_follower_does_not_guess_between_concurrent_sessions(repo):
    from gptme_agent_events.event import AgentEvent

    now = [100.0]
    w = GitWatcher()
    w.watch(str(repo))
    f = Follower([], git=w, clock=lambda: now[0])
    sha = commit(repo, "ambiguous", None)
    shells = [
        AgentEvent(
            kind="commit",
            ts="2099-01-01T00:00:00+00:00",
            harness="codex",
            session=s,
            detail={"dir": str(repo), "sha": sha[:7]},
        )
        for s in ("s1", "s2")
    ]
    assert f._with_git(shells, False) == []
    now[0] += 6
    events = f._with_git([], False)
    assert events[0].session == "" and events[0].harness == "git"
    assert {e.session for e in events[1:]} == {"s1", "s2"}


def test_repo_discovery_retries_transient_git_failure(repo, monkeypatch):
    from gptme_agent_events import gitwatch

    real_git = gitwatch._git
    monkeypatch.setattr(gitwatch, "_git", lambda args: None)
    w = GitWatcher()
    assert w.watch(str(repo)) is None
    monkeypatch.setattr(gitwatch, "_git", real_git)
    assert w.watch(str(repo)) == repo / ".git"


def test_follower_push_without_trailer_keeps_shell_attribution(repo, tmp_path):
    from gptme_agent_events.event import AgentEvent

    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("remote", "set-url", "origin", str(remote), cwd=repo)
    w = GitWatcher()
    w.watch(str(repo))
    f = Follower([], git=w)
    commit(repo, "push me", None)
    w.poll()  # consume the commit separately
    git("push", "-q", "origin", "main", cwd=repo)
    shell = AgentEvent(
        kind="push",
        ts="2099-01-01T00:00:00+00:00",
        harness="gptme",
        session="pusher",
        tool="shell",
        detail={"dir": str(repo)},
    )
    (event,) = f._with_git([shell], True)
    assert (event.kind, event.session, event.harness, event.tool) == (
        "push",
        "pusher",
        "gptme",
        "shell",
    )
    assert event.detail["source"] == "git"


def test_follower_never_overwrites_a_trailer_with_another_session(repo):
    from gptme_agent_events.event import AgentEvent

    now = [100.0]
    w = GitWatcher()
    w.watch(str(repo))
    f = Follower([], git=w, clock=lambda: now[0])
    sha = commit(repo, "owned", "author-session")
    shell = AgentEvent(
        kind="commit",
        ts="2099-01-01T00:00:00+00:00",
        harness="codex",
        session="other",
        detail={"dir": str(repo), "sha": sha[:7]},
    )
    events = f._with_git([shell], False)
    assert len(events) == 1 and events[0].session == "author-session"
    now[0] += 6
    assert f._with_git([], False) == []


def test_hashless_result_is_not_assigned_to_first_of_two_commits(repo):
    from gptme_agent_events.event import AgentEvent

    now = [100.0]
    w = GitWatcher()
    w.watch(str(repo))
    f = Follower([], git=w, clock=lambda: now[0])
    commit(repo, "one", None)
    commit(repo, "two", None)
    shell = AgentEvent(
        kind="commit",
        ts="2099-01-01T00:00:00+00:00",
        harness="codex",
        session="second",
        detail={"dir": str(repo)},
    )
    assert f._with_git([shell], False) == []
    now[0] += 6
    events = f._with_git([], False)
    assert [e.session for e in events[:2]] == ["", ""]


def test_git_reflog_rewrite_changing_old_sha_does_not_replay(repo, monkeypatch):
    monkeypatch.setitem(ENV, "GIT_COMMITTER_DATE", "2026-11-12T17:00:00+00:00")
    w = GitWatcher(clock=lambda: 1794502800.0)
    w.watch(str(repo))
    for msg in ("one", "two", "three"):
        commit(repo, msg, "s")
    assert len(w.poll()) == 3
    git("reflog", "delete", "--rewrite", "HEAD@{1}", cwd=repo)
    assert w.poll() == []
