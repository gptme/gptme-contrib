"""Tests for skills/end: the wrap-up gate (end-check.py) and exit helper."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "end"
CHECK = SKILL_DIR / "scripts" / "end-check.py"
EXIT = SKILL_DIR / "scripts" / "end-exit.py"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module importable
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def check():
    return _load(CHECK)


GIT_ISOLATION = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> str:
    """git with global hooks/signing disabled (the host's identity hook rejects temp repos)."""
    env = {**os.environ, "ALLOW_GIT_IDENTITY": "1"}
    return subprocess.run(
        ["git", *GIT_ISOLATION, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with an upstream so @{u} resolves."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hi\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "-u", "origin", "HEAD:master")
    return work


_DEFAULT_SESSION_ID = "test-fixture-session-00000000"


def run_check(work: Path, *extra: str, env: dict | None = None) -> tuple[int, dict]:
    # Inject a fake session ID so commit attribution works in CI (which has no
    # CC_SESSION_ID).  Tests that deliberately strip it pass env= explicitly.
    base_env = (
        {**os.environ, "CC_SESSION_ID": _DEFAULT_SESSION_ID} if env is None else env
    )
    p = subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--workspace",
            str(work),
            "--json",
            "--no-prs",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=base_env,
    )
    assert p.stdout, p.stderr
    return p.returncode, json.loads(p.stdout)


def _env_without_cc_session() -> dict:
    """Strip Claude Code session vars to simulate a gptme / Codex harness."""
    return {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "CC_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT",
            "CC_MODEL",
        )
    }


def _status(rep: dict, name: str) -> str | None:
    for c in rep["checks"]:
        if c["name"] == name:
            return str(c["status"])
    return None


def test_clean_repo_is_clean_light(repo: Path):
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 0
    assert rep["verdict"] == "CLEAN_LIGHT", rep
    assert _status(rep, "uncommitted") == "ok"
    assert _status(rep, "unpushed") == "ok"


def test_uncommitted_file_blocks(repo: Path):
    (repo / "new.txt").write_text("x\n")
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 2
    assert rep["verdict"] == "BLOCKED"
    assert _status(rep, "uncommitted") == "block"
    assert any("new.txt" in it for c in rep["checks"] for it in c["items"])


def test_old_dirt_is_info_not_block(repo: Path):
    """A dirty file older than the session window is another session's problem."""
    f = repo / "old.txt"
    f.write_text("x\n")
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).timestamp()
    os.utime(f, (old, old))
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 0, rep
    assert _status(rep, "uncommitted") == "ok"
    assert _status(rep, "uncommitted-other") == "info"


def test_staged_old_file_blocks(repo: Path):
    """A staged file with an old mtime (content pre-dates session) still blocks."""
    f = repo / "staged.txt"
    f.write_text("x\n")
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).timestamp()
    os.utime(f, (old, old))
    # Without staging: old file is "info" (other session's dirt)
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 0 and _status(rep, "uncommitted-other") == "info"
    # After explicit git add: staged index change must block regardless of mtime
    _git(repo, "add", "staged.txt")
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 2, rep
    assert _status(rep, "uncommitted") == "block"
    assert any("staged.txt" in it for c in rep["checks"] for it in c["items"])


def test_paths_scope_ignores_sibling_dirt(repo: Path):
    """--paths: dirt outside the declared scope is info even inside the time window."""
    (repo / "mine.txt").write_text("m\n")
    (repo / "theirs.txt").write_text("t\n")
    rc, rep = run_check(repo, "--since", "1h", "--paths", "theirs.txt")
    assert rc == 2 and _status(rep, "uncommitted") == "block"
    rc, rep = run_check(repo, "--since", "1h", "--paths", "docs/")
    assert rc == 0, rep
    assert _status(rep, "uncommitted") == "ok"
    assert _status(rep, "uncommitted-other") == "info"
    # absolute paths inside the repo are accepted too
    rc, rep = run_check(repo, "--since", "1h", "--paths", str(repo / "mine.txt"))
    assert rc == 2
    items = [
        it for c in rep["checks"] if c["name"] == "uncommitted" for it in c["items"]
    ]
    assert items == ["?? mine.txt"]


def test_unpushed_commit_blocks(repo: Path):
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "feat: a")
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 2
    assert _status(rep, "unpushed") == "block"
    assert any("feat: a" in c for c in rep["commits"])


def test_journal_required_when_work_happened(repo: Path):
    (repo / "journal").mkdir()
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "feat: a")
    _git(repo, "push", "-q")
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 2
    assert _status(rep, "journal") == "block"
    # Writing today's entry (and committing+pushing it) clears the gate.
    today = datetime.now().strftime("%Y-%m-%d")
    jf = repo / "journal" / today / "session.md"
    jf.parent.mkdir(parents=True)
    jf.write_text("# done\n")
    _git(repo, "add", str(jf))
    _git(repo, "commit", "-q", "-m", "journal")
    _git(repo, "push", "-q")
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 0, rep
    assert _status(rep, "journal") == "ok"


def test_substantial_session_stays_alive(repo: Path):
    for i in range(3):
        (repo / f"f{i}.txt").write_text(f"{i}\n")
        _git(repo, "add", f"f{i}.txt")
        _git(repo, "commit", "-q", "-m", f"feat: f{i}")
    _git(repo, "push", "-q")
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 0
    assert rep["verdict"] == "CLEAN_SUBSTANTIAL"
    rc, rep = run_check(repo, "--since", "1h", "--light-commits", "5")
    assert rep["verdict"] == "CLEAN_LIGHT"


def test_paths_scope_commits_unpushed_and_worktrees(repo: Path, tmp_path: Path):
    # A sibling session's unpushed commit outside the footprint → info, not block
    (repo / "theirs.txt").write_text("t\n")
    _git(repo, "add", "theirs.txt")
    _git(repo, "commit", "-q", "-m", "feat: theirs")
    rc, rep = run_check(repo, "--since", "1h", "--paths", "mine/")
    assert rc == 0, rep
    assert _status(rep, "unpushed") == "info"
    assert rep["commits"] == []  # commits are attributed by footprint too
    # Own commit inside the footprint still blocks until pushed
    (repo / "mine").mkdir()
    (repo / "mine" / "a.txt").write_text("a\n")
    _git(repo, "add", "mine/a.txt")
    _git(repo, "commit", "-q", "-m", "feat: mine")
    rc, rep = run_check(repo, "--since", "1h", "--paths", "mine/")
    assert rc == 2 and _status(rep, "unpushed") == "block"
    assert [c for c in rep["commits"] if "feat: mine" in c] and not [
        c for c in rep["commits"] if "feat: theirs" in c
    ]
    _git(repo, "push", "-q")
    # Undeclared dirty worktree → info; declared (absolute path in --paths) → block
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    (wt / "wip.txt").write_text("wip\n")
    rc, rep = run_check(repo, "--since", "1h", "--paths", "mine/")
    assert rc == 0 and _status(rep, "worktrees") == "info"
    rc, rep = run_check(repo, "--since", "1h", "--paths", "mine/", str(wt))
    assert rc == 2 and _status(rep, "worktrees") == "block"


def test_dirty_worktree_blocks(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    (wt / "wip.txt").write_text("wip\n")
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 2
    assert _status(rep, "worktrees") == "block"
    assert any(str(wt) in it for c in rep["checks"] for it in c["items"])


def test_parse_since(check):
    now = datetime.now(timezone.utc)
    assert now - check.parse_since("90m") < timedelta(minutes=91)
    assert check.parse_since("2026-01-01T00:00:00Z").tzinfo is not None
    assert check.parse_since("2026-01-01T00:00:00").tzinfo is not None


def test_classify_cmdline(check):
    assert (
        check._classify_cmdline(["claude", "--dangerously-skip-permissions"])
        == "claude-code"
    )
    assert (
        check._classify_cmdline(["node", "/usr/lib/node_modules/.bin/claude"])
        == "claude-code"
    )
    assert check._classify_cmdline(["/home/x/.local/bin/gptme", "-n"]) == "gptme"
    assert check._classify_cmdline(["codex"]) == "codex"
    assert check._classify_cmdline(["bash", "-c", "claude"]) is None
    assert check._classify_cmdline([]) is None


def test_exit_dry_run_never_signals(tmp_path: Path):
    """--dry-run with an explicit pid reports the target and sends nothing."""
    victim = subprocess.Popen(["sleep", "30"])
    try:
        p = subprocess.run(
            [
                sys.executable,
                str(EXIT),
                "--dry-run",
                "--pid",
                str(victim.pid),
                "--even-if-noninteractive",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert p.returncode == 0, p.stderr
        assert "dry-run" in p.stdout
        assert victim.poll() is None
    finally:
        victim.kill()


def test_no_session_id_recent_commit_not_attributed(tmp_path: Path):
    """P1: without a session_id (gptme/Codex env), recent commits must not block.

    When no CC_SESSION_ID is present, `check_commits` cannot distinguish this
    session's commits from a sibling's or from pre-session repository activity.
    The old code attributed every time-window commit to this session, which
    blocked sessions that did nothing just because the repo had recent commits.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hi\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-q", "-m", "feat: pre-session commit")
    _git(work, "push", "-q", "-u", "origin", "HEAD:master")
    # Journal dir exists; this is what triggers the false BLOCKED when
    # did_work=True leaks in from unattributed commits.
    (work / "journal").mkdir()
    # Strip CC session env vars to simulate gptme / Codex (no session_id).
    env = _env_without_cc_session()
    rc, rep = run_check(work, "--since", "1h", env=env)
    assert rc == 0, rep
    assert rep["commits"] == [], "without session_id, no commits should be attributed"


def test_root_commit_files_changed_counted(tmp_path: Path):
    """P2: a session whose only commit is the root commit must have files_changed > 0."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    for i in range(15):
        (work / f"f{i}.txt").write_text(f"{i}\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "feat: init 15 files")
    _git(work, "push", "-q", "-u", "origin", "HEAD:master")
    rc, rep = run_check(work, "--since", "1h")
    assert rc == 0, rep
    assert (
        rep["files_changed"] >= 15
    ), "root commit files must be counted via empty-tree diff"
    assert rep["verdict"] == "CLEAN_SUBSTANTIAL"


def test_exit_signals_target_after_delay():
    victim = subprocess.Popen(["sleep", "30"])
    try:
        p = subprocess.run(
            [
                sys.executable,
                str(EXIT),
                "--pid",
                str(victim.pid),
                "--delay",
                "0.2",
                "--even-if-noninteractive",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert p.returncode == 0, p.stderr
        assert victim.wait(timeout=5) == -15  # SIGTERM
    finally:
        if victim.poll() is None:
            victim.kill()
