"""Commits and pushes come from git's reflogs, attributed by Git-Session-Id."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from gptme_agent_events import Follower
from gptme_agent_events.extractors import github_slug
from gptme_agent_events.follow import claude_code_source
from gptme_agent_events.gitwatch import GitWatcher

ENV = {
    **os.environ,
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
    assert ev.detail == {"sha": sha[:12], "source": "git", "repo": "gptme/gptme"}
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


def test_follower_reports_one_commit_from_git_with_session_harness(repo, tmp_path):
    """The shell-detected commit is replaced by the git one: no double chime."""
    github_slug.cache_clear()
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
    sha = commit(repo, "via agent", "sess-cc")
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
