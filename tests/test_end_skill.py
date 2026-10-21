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
# Commit-message trailers: the default session owns a commit; a sibling owns another.
OWN_TRAILER = f"Session: {_DEFAULT_SESSION_ID}"
OTHER_TRAILER = "Session: sibling-session-99999999"


def run_check(work: Path, *extra: str, env: dict | None = None) -> tuple[int, dict]:
    # Inject a fake session ID so commit attribution works in CI (which has no
    # CC_SESSION_ID).  Other session vars are stripped first: a live harness
    # exports CLAUDE_CODE_SESSION_ID, which the gate prefers over CC_SESSION_ID.
    # Tests that deliberately strip it pass env= explicitly.
    base_env = (
        {**_env_without_cc_session(), "CC_SESSION_ID": _DEFAULT_SESSION_ID}
        if env is None
        else env
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


def _commit(
    work: Path, subject: str, files: list[str], trailer: str | None = OWN_TRAILER
) -> str:
    """Write `files`, commit them, and return the new HEAD SHA.

    `trailer` goes in the message body: OWN_TRAILER (default) marks the commit as
    this test session's, OTHER_TRAILER marks a sibling's, None leaves it untagged.
    """
    for rel in files:
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{subject} {rel}\n")
    _git(work, "add", "--", *files)
    args = ["commit", "-q", "-m", subject]
    if trailer:
        args += ["-m", trailer]
    _git(work, *args)
    return _git(work, "rev-parse", "HEAD")


def _push(work: Path) -> None:
    _git(work, "push", "-q")


def _env_without_cc_session() -> dict:
    """Strip Claude Code session vars to simulate a gptme / Codex harness."""
    return {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "CC_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
            "GPTME_SESSION_ID",
            "CODEX_THREAD_ID",
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
    _commit(repo, "feat: a", ["a.txt"])
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 2
    assert _status(rep, "unpushed") == "block"
    assert any("feat: a" in c for c in rep["commits"])


def test_journal_required_when_work_happened(repo: Path):
    (repo / "journal").mkdir()
    _commit(repo, "feat: a", ["a.txt"])
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
        _commit(repo, f"feat: f{i}", [f"f{i}.txt"])
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


def test_no_session_id_recent_commit_not_attributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    # Simulate a native runtime, then strip every supported session ID.
    for name in (
        "CLAUDE_CODE_SESSION_ID",
        "CC_SESSION_ID",
        "GPTME_SESSION_ID",
        "CODEX_THREAD_ID",
    ):
        monkeypatch.setenv(name, "native-runtime-session")
    env = _env_without_cc_session()
    assert (
        not {
            "CLAUDE_CODE_SESSION_ID",
            "CC_SESSION_ID",
            "GPTME_SESSION_ID",
            "CODEX_THREAD_ID",
        }
        & env.keys()
    )
    rc, rep = run_check(work, "--since", "1h", env=env)
    assert rc == 0, rep
    assert rep["commits"] == [], "without session_id, no commits should be attributed"


def test_known_session_sibling_commit_not_attributed(repo: Path):
    """A known session id with no matching tag must not fall back to the time window."""
    _commit(repo, "feat: sibling", ["sibling.txt"], trailer=OTHER_TRAILER)
    _push(repo)
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 0, rep
    assert rep["commits"] == []
    assert rep["files_changed"] == 0
    assert rep["verdict"] == "CLEAN_LIGHT"


def test_missing_session_id_ignores_sibling_files(repo: Path):
    """No session id: a sibling's commit (>10 files) is neither listed nor counted."""
    _commit(repo, "feat: sibling bulk", [f"s{i}.txt" for i in range(12)], trailer=None)
    _push(repo)
    rc, rep = run_check(repo, "--since", "1h", env=_env_without_cc_session())
    assert rc == 0, rep
    assert rep["commits"] == []
    assert rep["files_changed"] == 0
    assert rep["verdict"] == "CLEAN_LIGHT"


def test_interleaved_own_and_sibling_history_counts_own_files_exactly(repo: Path):
    """Own/sibling/own: only own commits count; files are unique across own diffs."""
    _commit(repo, "feat: own one", ["a.txt", "b.txt"])
    _commit(repo, "feat: sibling", ["c.txt", "d.txt"], trailer=OTHER_TRAILER)
    _commit(repo, "feat: own two", ["b.txt", "e.txt"])
    _push(repo)
    rc, rep = run_check(repo, "--since", "1h")
    assert rc == 0, rep
    subjects = sorted(c.split(" ", 1)[1] for c in rep["commits"])
    assert subjects == ["feat: own one", "feat: own two"]
    # a, b, e (b is touched by both own commits): the sibling's c/d are excluded.
    assert rep["files_changed"] == 3


def test_paths_ownership_counts_untagged_commits_inside_scope(repo: Path):
    """--paths is explicit ownership: untagged in-scope commits count, outside ones don't."""
    _commit(repo, "feat: mine a", ["mine/a.txt", "mine/b.txt"], trailer=None)
    _commit(repo, "feat: theirs", ["theirs/t.txt"], trailer=None)
    _commit(repo, "feat: mixed", ["mine/c.txt", "theirs/u.txt"], trailer=None)
    _push(repo)
    rc, rep = run_check(repo, "--since", "1h", "--paths", "mine/")
    assert rc == 0, rep
    subjects = sorted(c.split(" ", 1)[1] for c in rep["commits"])
    assert subjects == ["feat: mine a", "feat: mixed"]
    # Only mine/a, mine/b, mine/c count: theirs/* is outside the declared scope.
    assert rep["files_changed"] == 3


@pytest.mark.parametrize("scope", [".", "./", "absolute"])
def test_repository_root_scope_counts_all_declared_work(repo: Path, scope: str):
    """A root footprint owns every changed file, including untagged commits."""
    _commit(
        repo, "feat: root footprint", [f"src/f{i}.txt" for i in range(15)], trailer=None
    )
    _push(repo)
    root_scope = str(repo) if scope == "absolute" else scope
    rc, rep = run_check(repo, "--since", "1h", "--paths", root_scope)
    assert rc == 0, rep
    # The recent fixture root commit (README.md) also belongs to this footprint.
    assert len(rep["commits"]) == 2
    assert rep["files_changed"] == 16
    assert rep["verdict"] == "CLEAN_SUBSTANTIAL"
    # The same declared root must still enforce the journal gate.
    (repo / "journal").mkdir()
    rc, rep = run_check(repo, "--since", "1h", "--paths", root_scope)
    assert rc == 2, rep
    assert _status(rep, "journal") == "block"


@pytest.mark.parametrize("scoped", [False, True])
def test_owned_merge_counts_first_parent_changes(repo: Path, scoped: bool):
    """An owned merge of old work must not look light or count mainline siblings."""
    mainline = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(
        repo,
        "feat: old feature",
        [f"feature/f{i}.txt" for i in range(15)],
        trailer=None,
    )
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    subprocess.run(
        ["git", *GIT_ISOLATION, "commit", "--amend", "--no-edit", "--date", old],
        cwd=repo,
        env={**os.environ, "GIT_COMMITTER_DATE": old, "ALLOW_GIT_IDENTITY": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    _git(repo, "checkout", "-q", mainline)
    _commit(repo, "feat: sibling mainline", ["sibling.txt"], trailer=OTHER_TRAILER)
    _git(
        repo,
        "merge",
        "--no-ff",
        "feature",
        "-m",
        "feat: owned merge",
        "-m",
        OWN_TRAILER,
    )
    _push(repo)

    extra = ["--paths", "feature/"] if scoped else []
    rc, rep = run_check(repo, "--since", "1h", *extra)
    assert rc == 0, rep
    assert len(rep["commits"]) == 1
    assert "owned merge" in rep["commits"][0]
    assert rep["files_changed"] == 15
    assert rep["verdict"] == "CLEAN_SUBSTANTIAL"


def test_owned_root_commit_counts_its_files(tmp_path: Path):
    """P2: a session whose only commit is the root commit must have files_changed > 0."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _commit(work, "feat: init 15 files", [f"f{i}.txt" for i in range(15)])
    _git(work, "push", "-q", "-u", "origin", "HEAD:master")
    rc, rep = run_check(work, "--since", "1h")
    assert rc == 0, rep
    assert len(rep["commits"]) == 1
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


# --------------------------------------------------------------------------- #
# PR ownership: only --pr URLs are checked; gh is faked, never called for real
# --------------------------------------------------------------------------- #

PR_URL = "https://github.com/gptme/gptme-contrib/pull/1954"
PR_URL_2 = "https://github.com/gptme/gptme-contrib/pull/1955"
PR_META = {
    "url": PR_URL,
    "title": "fix(end): attribution",
    "number": 1954,
    "headRefName": "fix/end-session-attribution-685b",
}


class FakeGh:
    """Stands in for subprocess.run for `gh pr view` / `gh pr checks`.

    `views` / `checks` map a URL to (returncode, stdout) or to an exception to raise.
    Any other gh invocation is a test failure.
    """

    def __init__(self, views=None, checks=None):
        self.views = views or {}
        self.checks = checks or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            table = self.views
        elif argv[:3] == ["gh", "pr", "checks"]:
            table = self.checks
        else:
            raise AssertionError(f"unexpected subprocess call: {argv}")
        outcome = table.get(argv[3], (1, ""))
        if isinstance(outcome, BaseException):
            raise outcome
        rc, out = outcome
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def _patch_gh(monkeypatch: pytest.MonkeyPatch, check, fake: FakeGh) -> None:
    monkeypatch.setattr(check.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(check.subprocess, "run", fake)


def _report(check, root: Path, since: datetime | None = None):
    return check.Report(
        harness=check.Harness(), root=root, since=since, since_source="test"
    )


def _prs_check(rep):
    return next(c for c in rep.checks if c.name == "prs")


def _view_urls(fake: FakeGh) -> list[str]:
    return [c[3] for c in fake.calls if c[:3] == ["gh", "pr", "view"]]


def _checks_urls(fake: FakeGh) -> list[str]:
    return [c[3] for c in fake.calls if c[:3] == ["gh", "pr", "checks"]]


def test_no_declared_pr_makes_no_gh_call(check, tmp_path: Path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError(f"unexpected external call: {args}")

    monkeypatch.setattr(check.subprocess, "run", boom)
    monkeypatch.setattr(check.shutil, "which", boom)
    rep = _report(check, tmp_path)
    check.check_prs(rep, [])
    assert [c.status for c in rep.checks] == ["info"]
    assert "no session-owned PRs declared" in rep.checks[0].summary
    assert rep.prs == []


def test_cli_without_pr_declaration_never_calls_gh(repo: Path, tmp_path: Path):
    env, log = _fake_gh_env(tmp_path)
    p = _run_cli(repo, "--since", "1h", env=env)
    assert p.returncode == 0, p.stderr
    assert not log.exists(), "no --pr means no gh invocation at all"
    rep = json.loads(p.stdout)
    assert _status(rep, "prs") == "info"


def test_declared_pr_with_failing_ci_warns(check, tmp_path: Path, monkeypatch):
    fake = FakeGh(
        views={PR_URL: (0, json.dumps(PR_META))},
        checks={PR_URL: (1, "build\tfail\t2m\n")},
    )
    _patch_gh(monkeypatch, check, fake)
    # Declared PRs count even when opened before this session started (resumed work).
    rep = _report(check, tmp_path, since=datetime.now(timezone.utc))
    check.check_prs(rep, [PR_URL])
    status = _prs_check(rep)
    assert status.status == "warn"
    assert "failing CI" in status.summary
    assert [pr["url"] for pr in rep.prs] == [PR_URL]
    assert _view_urls(fake) == [PR_URL]
    assert _checks_urls(fake) == [PR_URL]


def test_declared_green_pr_is_info(check, tmp_path: Path, monkeypatch):
    fake = FakeGh(
        views={PR_URL: (0, json.dumps(PR_META))},
        checks={PR_URL: (0, "build\tpass\t2m\n")},
    )
    _patch_gh(monkeypatch, check, fake)
    rep = _report(check, tmp_path)
    check.check_prs(rep, [PR_URL])
    assert _prs_check(rep).status == "info"
    assert rep.prs[0]["title"] == PR_META["title"]
    assert rep.prs[0]["headRefName"] == PR_META["headRefName"]


def test_repeated_pr_urls_are_checked_once(check, tmp_path: Path, monkeypatch):
    fake = FakeGh(
        views={
            PR_URL: (0, json.dumps(PR_META)),
            PR_URL_2: (0, json.dumps({**PR_META, "url": PR_URL_2, "number": 1955})),
        },
        checks={PR_URL: (0, ""), PR_URL_2: (0, "")},
    )
    _patch_gh(monkeypatch, check, fake)
    rep = _report(check, tmp_path)
    check.check_prs(rep, [PR_URL, PR_URL, PR_URL_2, PR_URL])
    assert _view_urls(fake) == [PR_URL, PR_URL_2]
    assert _checks_urls(fake) == [PR_URL, PR_URL_2]
    assert len(rep.prs) == 2


def test_failed_or_malformed_gh_lookups_never_crash(check, tmp_path: Path, monkeypatch):
    base = "https://github.com/gptme/gptme-contrib/pull"
    ok, rc_fail, bad_json, not_dict, sparse, timed_out = (
        f"{base}/{n}" for n in (1, 2, 3, 4, 5, 6)
    )
    fake = FakeGh(
        views={
            ok: (0, json.dumps({**PR_META, "url": ok, "number": 1})),
            rc_fail: (1, ""),
            bad_json: (0, "not json"),
            not_dict: (0, "[]"),
            sparse: (0, "{}"),
            timed_out: subprocess.TimeoutExpired(cmd="gh", timeout=20),
        },
        checks={ok: (0, ""), sparse: (0, "")},
    )
    _patch_gh(monkeypatch, check, fake)
    rep = _report(check, tmp_path)
    check.check_prs(rep, [ok, rc_fail, bad_json, not_dict, sparse, timed_out])
    status = _prs_check(rep)
    assert status.status == "warn"
    # sparse metadata is tolerated; the four broken lookups are reported, not listed as PRs
    assert [pr["url"] for pr in rep.prs] == [ok, sparse]
    for label in ("#2", "#3", "#4", "#6"):
        assert any(label in item for item in status.items), label
    assert _checks_urls(fake) == [ok, sparse], "no gh pr checks for failed lookups"


def test_non_pr_urls_never_reach_gh(check, tmp_path: Path, monkeypatch):
    fake = FakeGh()
    _patch_gh(monkeypatch, check, fake)
    rep = _report(check, tmp_path)
    check.check_prs(
        rep,
        [
            "https://github.com/gptme/gptme-contrib/issues/1954",
            "https://example.com/gptme/gptme-contrib/pull/1954",
        ],
    )
    assert fake.calls == []
    assert _prs_check(rep).status == "warn"
    assert rep.prs == []


def _fake_gh_env(tmp_path: Path) -> tuple[dict, Path]:
    """PATH with a fake `gh` that logs its argv; returns (env, log path)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + _FAKE_GH_BODY)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    env = {
        **_env_without_cc_session(),
        "CC_SESSION_ID": _DEFAULT_SESSION_ID,
        "FAKE_GH_LOG": str(log),
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    return env, log


_FAKE_GH_BODY = """
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["pr", "view"]:
    print(json.dumps({"url": sys.argv[3], "title": "owned", "number": 1954, "headRefName": "fix/x"}))
    sys.exit(0)
if sys.argv[1:3] == ["pr", "checks"]:
    print("build\\tfail\\t2m")
    sys.exit(1)
sys.exit(1)
"""


def _run_cli(work: Path, *extra: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECK), "--workspace", str(work), "--json", *extra],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.parametrize(
    "bad",
    [
        "https://github.com/gptme/gptme-contrib/issues/1954",
        "https://example.com/gptme/gptme-contrib/pull/1954",
        "https://github.com/gptme/gptme-contrib/pull/abc",
        "gptme/gptme-contrib#1954",
    ],
)
def test_pr_argument_rejects_non_pr_urls(repo: Path, tmp_path: Path, bad: str):
    env, log = _fake_gh_env(tmp_path)
    p = _run_cli(repo, "--pr", bad, env=env)
    assert p.returncode == 2, p.stdout
    assert p.stdout == ""
    assert "GitHub PR URL" in p.stderr
    assert not log.exists(), "a rejected URL must never reach gh"


def test_cli_declared_pr_checked_once_and_failing_ci_warns(repo: Path, tmp_path: Path):
    env, log = _fake_gh_env(tmp_path)
    p = _run_cli(repo, "--since", "1h", "--pr", PR_URL, "--pr", PR_URL, env=env)
    assert p.returncode == 0, p.stderr  # a red PR warns; it does not block
    rep = json.loads(p.stdout)
    assert _status(rep, "prs") == "warn"
    assert [pr["url"] for pr in rep["prs"]] == [PR_URL]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [c[:2] for c in calls] == [["pr", "view"], ["pr", "checks"]]
    assert rep["verdict"] == "CLEAN_SUBSTANTIAL"


def test_no_prs_overrides_declared_pr(repo: Path, tmp_path: Path):
    env, log = _fake_gh_env(tmp_path)
    p = _run_cli(repo, "--since", "1h", "--pr", PR_URL, "--no-prs", env=env)
    assert p.returncode == 0, p.stderr
    assert not log.exists()
    assert _status(json.loads(p.stdout), "prs") is None


def test_paths_exclude_tagged_commits_outside_footprint(repo: Path):
    _commit(repo, "feat: own outside footprint", ["outside.txt"])
    _commit(repo, "feat: own scoped", ["mine/a with spaces.txt"])
    _push(repo)
    rc, rep = run_check(repo, "--since", "1h", "--paths", "mine/")
    assert rc == 0, rep
    assert len(rep["commits"]) == 1
    assert "own scoped" in rep["commits"][0]
    assert rep["files_changed"] == 1


def test_declared_pr_lookup_failure_cannot_be_light(check, tmp_path: Path, monkeypatch):
    fake = FakeGh(views={PR_URL: (1, "")})
    _patch_gh(monkeypatch, check, fake)
    rep = _report(check, tmp_path)
    check.check_prs(rep, [PR_URL])
    check.decide(rep, 2, 10)
    assert rep.verdict == "CLEAN_SUBSTANTIAL"
    assert _prs_check(rep).status == "warn"
