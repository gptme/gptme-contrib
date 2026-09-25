"""Smoke tests for the worktree push guard (contrib port, ErikBjare/alice#83)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from gptme_coordination.worktree_guard import (
    legacy_pr_branch_key,
    origin_slug,
    pushed_branches,
)

CONTRIB_ROOT = Path(__file__).resolve().parents[3]


def test_legacy_key_unqualifies_repo_qualified_branch_claim() -> None:
    assert legacy_pr_branch_key("pr-branch:org/repo#feat") == "pr-branch:feat"
    assert legacy_pr_branch_key("pr-branch:feat") is None
    assert legacy_pr_branch_key("github:org/repo#1") is None
    # Branch names may contain "#" (repo names cannot): split on the FIRST "#".
    assert legacy_pr_branch_key("pr-branch:org/repo#feat#x") == "pr-branch:feat#x"


def test_origin_slug() -> None:
    assert origin_slug("git@github.com:org/repo.git") == "org/repo"
    assert origin_slug("https://github.com/org/repo") == "org/repo"
    assert origin_slug("http://github.com/org/repo.git") == "org/repo"
    assert origin_slug("ssh://git@github.com/org/repo.git") == "org/repo"
    assert origin_slug("HTTPS://GITHUB.COM/Org/Repo.git") == "org/repo"
    assert origin_slug("https://gitlab.com/org/repo.git") is None
    assert origin_slug("https://evil.com/github.com/org/repo") is None


def test_origin_slug_rejects_unsupported_github_url_forms() -> None:
    """Unsupported URL syntax fails open instead of creating a wrong key."""
    assert origin_slug("https://user:token@github.com/org/repo.git") is None
    assert origin_slug("https://github.com:443/org/repo.git") is None
    assert origin_slug("https://github.com/org/repo/") is None


def test_initial_marker_is_never_exposed_partially(tmp_path: Path, monkeypatch) -> None:
    from gptme_coordination import worktree_guard

    git_dir = tmp_path / "git"
    git_dir.mkdir()
    real_link = worktree_guard.os.link

    def inspect_before_install(source: Path, destination: Path) -> None:
        assert json.loads(Path(source).read_text())["session_id"] == "session-a"
        assert not Path(destination).exists()
        real_link(source, destination)

    monkeypatch.setattr(worktree_guard.os, "link", inspect_before_install)
    assert worktree_guard.write_marker_atomic_new(git_dir, "session-a", 123, "agent-a")
    assert (
        json.loads((git_dir / worktree_guard.MARKER_NAME).read_text())["session_id"]
        == "session-a"
    )
    assert list(git_dir.glob(f"{worktree_guard.MARKER_NAME}.*.tmp")) == []


def test_pushed_branches_filters_deletes_and_non_heads() -> None:
    lines = [
        "refs/heads/feat abc123 refs/heads/feat def456",
        "refs/heads/gone " + "0" * 40 + " refs/heads/gone def456",  # deletion
        "refs/tags/v1 abc123 refs/tags/v1 def456",  # not a branch
    ]
    assert pushed_branches(lines) == ["feat"]


def _copied_entry_point_without_package(tmp_path: Path, script_name: str) -> Path:
    """Copy an entry point away from contrib so its package lookup must fail."""
    script = tmp_path / script_name
    script.write_text((CONTRIB_ROOT / "scripts/hooks" / script_name).read_text())
    return script


def test_entry_point_uses_pushed_remote_url(tmp_path: Path) -> None:
    """Git's argv URL, not the checkout's ``origin``, reaches the guard."""
    script = tmp_path / "worktree-push-guard"
    script.write_text((CONTRIB_ROOT / "scripts/hooks/worktree-push-guard").read_text())
    package = tmp_path / "gptme_coordination"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "worktree_guard.py").write_text(
        "import pathlib\n"
        "def run_push_guard(lines, *, remote_url=None):\n"
        "    pathlib.Path(__file__).with_name('seen').write_text(remote_url or '')\n"
        "    return 0\n"
    )
    remote_url = "ssh://git@github.com/org/mirror.git"
    result = subprocess.run(
        [sys.executable, str(script), "mirror", remote_url],
        input="refs/heads/feat abc refs/heads/feat def\n",
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "AGENT_WORKSPACE": str(tmp_path),
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (package / "seen").read_text() == remote_url


def test_entry_point_fails_open_on_incompatible_module(tmp_path: Path) -> None:
    """Missing ``run_push_guard`` is startup failure, never a deny."""
    script = tmp_path / "worktree-push-guard"
    script.write_text((CONTRIB_ROOT / "scripts/hooks/worktree-push-guard").read_text())
    package = tmp_path / "gptme_coordination"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "worktree_guard.py").write_text("INCOMPATIBLE = True\n")
    result = subprocess.run(
        [sys.executable, str(script), "origin", "git@github.com:org/repo.git"],
        input="refs/heads/feat abc refs/heads/feat def\n",
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "AGENT_WORKSPACE": str(tmp_path),
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "skipping (import error:" in result.stderr


def test_entry_point_fails_open_without_package(tmp_path: Path) -> None:
    """Guard entry point exits 0 when the coordination package is missing."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "AGENT_WORKSPACE": "",
        "BOB_WORKSPACE": "",
        "BOB_BRAIN_ROOT": "",
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "BOB_SESSION_ID": "session-under-test",
        "GIT_COMMITTER_SESSION_ID": "",
        "BOB_AUTONOMOUS_AGENT_ID": "agent-under-test",
    }
    script = _copied_entry_point_without_package(tmp_path, "worktree-push-guard")
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        input="refs/heads/feat abc refs/heads/feat def\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "skipping (import error:" in result.stderr


def test_env_flag_rejects_zero_and_false(monkeypatch) -> None:
    """A conventional `=0` must not switch a guard on (Greptile P2)."""
    from gptme_coordination.worktree_guard import _env_flag

    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("GUARD_FLAG", value)
        assert _env_flag("GUARD_FLAG") is True, value

    for value in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("GUARD_FLAG", value)
        assert _env_flag("GUARD_FLAG") is False, value

    monkeypatch.delenv("GUARD_FLAG", raising=False)
    assert _env_flag("GUARD_FLAG") is False


def test_claim_expired_handles_naive_and_aware() -> None:
    from datetime import datetime, timedelta, timezone

    from gptme_coordination.work import WorkClaim
    from gptme_coordination.worktree_guard import _claim_expired

    now = datetime.now(timezone.utc)

    def claim(expires_at):
        return WorkClaim(
            task_id="t",
            claimer="other",
            epoch=1,
            claimed_at=now,
            expires_at=expires_at,
            status="claimed",
        )

    assert _claim_expired(claim(now - timedelta(minutes=1))) is True
    assert (
        _claim_expired(claim(now.replace(tzinfo=None) - timedelta(minutes=1))) is True
    )
    assert _claim_expired(claim(now + timedelta(minutes=1))) is False
    assert _claim_expired(claim(None)) is False


def test_session_pid_requires_launcher_pid(monkeypatch) -> None:
    from gptme_coordination.worktree_guard import _get_session_pid

    monkeypatch.delenv("BOB_SESSION_PID", raising=False)
    assert _get_session_pid() == 0
    monkeypatch.setenv("BOB_SESSION_PID", "12345")
    assert _get_session_pid() == 12345


def test_brain_root_honors_workspace_env(monkeypatch, tmp_path: Path) -> None:
    from gptme_coordination.worktree_guard import _get_brain_root

    workspace = tmp_path / "custom-workspace"
    monkeypatch.setenv("BOB_BRAIN_ROOT", str(tmp_path / "legacy-brain"))
    monkeypatch.setenv("BOB_WORKSPACE", str(workspace))
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "other"))
    assert _get_brain_root() == workspace


def test_push_guard_uses_workspace_db_env(monkeypatch, tmp_path: Path) -> None:
    from gptme_coordination.db import CoordinationDB
    from gptme_coordination.work import WorkClaimManager
    from gptme_coordination.worktree_guard import run_push_guard

    workspace = tmp_path / "custom-workspace"
    monkeypatch.delenv("COORDINATION_DB", raising=False)
    monkeypatch.setenv("BOB_BRAIN_ROOT", str(tmp_path / "legacy-brain"))
    monkeypatch.setenv("BOB_WORKSPACE", str(workspace))
    monkeypatch.delenv("AGENT_WORKSPACE", raising=False)

    rc = run_push_guard(
        ["refs/heads/feat abc123 refs/heads/feat def456"],
        worktree_root=tmp_path / "wt",
        remote_url="git@github.com:org/repo.git",
        session_id="sess-1",
        agent_id="agent-b",
        deny=False,
    )
    assert rc == 0

    db_path = workspace / "state" / "coordination" / "coord.db"
    with CoordinationDB(db_path) as db:
        claim = WorkClaimManager(db).get("pr-branch:org/repo#feat")
    assert claim is not None
    assert claim.claimer == "agent-b"


def test_push_guard_defaults_to_brain_root_db(monkeypatch, tmp_path: Path) -> None:
    from gptme_coordination.db import CoordinationDB
    from gptme_coordination.work import WorkClaimManager
    from gptme_coordination.worktree_guard import run_push_guard

    brain_root = tmp_path / "brain"
    worktree = tmp_path / "worktree"
    monkeypatch.delenv("COORDINATION_DB", raising=False)
    monkeypatch.delenv("BOB_WORKSPACE", raising=False)
    monkeypatch.delenv("AGENT_WORKSPACE", raising=False)
    monkeypatch.delenv("BOB_BRAIN_ROOT", raising=False)

    rc = run_push_guard(
        ["refs/heads/feat abc123 refs/heads/feat def456"],
        worktree_root=worktree,
        brain_root=brain_root,
        remote_url="git@github.com:org/repo.git",
        session_id="sess-1",
        agent_id="agent-b",
        deny=False,
    )
    assert rc == 0

    db_path = brain_root / "state" / "coordination" / "coord.db"
    with CoordinationDB(db_path) as db:
        claim = WorkClaimManager(db).get("pr-branch:org/repo#feat")
    assert claim is not None
    assert claim.claimer == "agent-b"
    assert not (worktree / "state" / "coordination" / "coord.db").exists()


def test_dead_pid_without_authoritative_agent_id_is_dead() -> None:
    """A dead PID must not stay 'alive' on a synthesized marker agent id."""
    from gptme_coordination.worktree_guard import _is_holder_alive

    dead_pid = 2**22  # > pid_max on Linux default configurations
    assert _is_holder_alive({"pid": dead_pid, "agent_id": ""}) is False
    assert _is_holder_alive({"pid": dead_pid, "agent_id": "bob-autonomous-x"}) is True


def test_sequential_items_from_same_executor_do_not_collide(
    tmp_path: Path, monkeypatch
) -> None:
    from gptme_coordination.worktree_guard import run_guard

    worktree = tmp_path / "worktrees" / "pr"
    git_dir = worktree / "git"
    git_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "gptme_coordination.worktree_guard._WORKTREE_PREFIX",
        f"{tmp_path}/worktrees/",
    )

    assert (
        run_guard(
            worktree_root=worktree,
            git_dir=git_dir,
            brain_root=tmp_path,
            session_id="item-1",
            pid=123,
            agent_id="executor-1",
        )
        == 0
    )
    assert (
        run_guard(
            worktree_root=worktree,
            git_dir=git_dir,
            brain_root=tmp_path,
            session_id="item-2",
            pid=123,
            agent_id="executor-1",
        )
        == 0
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "state/coordination/worktree-guard.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [row["type"] for row in rows] == ["adopt"]


def test_worktree_guard_locates_only_owning_checkout(
    monkeypatch, tmp_path: Path
) -> None:
    """AGENT_WORKSPACE is data, not a trusted Python import root."""
    import importlib.machinery
    import importlib.util

    malicious = tmp_path / "malicious" / "packages" / "gptme-coordination" / "src"
    malicious.mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "malicious"))

    script = CONTRIB_ROOT / "scripts" / "hooks" / "worktree-guard"
    loader = importlib.machinery.SourceFileLoader(
        "test_worktree_guard_hook", str(script)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    before = list(sys.path)
    try:
        module._locate_package()
        trusted = CONTRIB_ROOT / "packages" / "gptme-coordination" / "src"
        assert sys.path[0] == str(trusted)
        assert str(malicious) not in sys.path
    finally:
        sys.path[:] = before


def test_worktree_guard_entry_point_fails_open_without_package(
    tmp_path: Path,
) -> None:
    """Post-commit occupancy entry point exits 0 when the package is missing."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "AGENT_WORKSPACE": "",
        "BOB_WORKSPACE": "",
        "BOB_BRAIN_ROOT": "",
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "BOB_SESSION_ID": "session-under-test",
        "GIT_COMMITTER_SESSION_ID": "",
        "BOB_AUTONOMOUS_AGENT_ID": "agent-under-test",
    }
    script = _copied_entry_point_without_package(tmp_path, "worktree-guard")
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "skipping (import error:" in result.stderr


def test_dead_holder_takeover_revalidates_under_lock(
    tmp_path: Path, monkeypatch
) -> None:
    from gptme_coordination import worktree_guard

    git_dir = tmp_path / "git"
    git_dir.mkdir()
    old = {
        "session_id": "dead",
        "pid": 2**22,
        "agent_id": "",
        "started_at": "old",
        "takeovers": [],
    }
    current = {
        "session_id": "winner",
        "pid": 123,
        "agent_id": "winner-agent",
        "started_at": "new",
        "takeovers": [],
    }
    marker = git_dir / worktree_guard.MARKER_NAME
    marker.write_text(json.dumps(current))
    monkeypatch.setattr(worktree_guard, "is_pid_alive", lambda pid: pid == 123)

    wrote = worktree_guard._update_marker_with_history(
        git_dir,
        "loser",
        456,
        "loser-agent",
        {"from": "dead", "to": "loser", "at": "now", "reason": "dead"},
        expected_holder=old,
    )

    assert wrote is False
    assert json.loads(marker.read_text()) == current


def test_legacy_alias_does_not_skip_qualified_claim(tmp_path: Path) -> None:
    """A live legacy alias must still result in a qualified claim.

    Regression for Greptile P1 on gptme/gptme-contrib#1693: the legacy-alias
    branch logged "claiming <key>" and then `continue`d, so the repo-qualified
    claim was never created and later sibling pushes had nothing to collide
    with. The alias is informational only; the qualified claim must still land.
    """
    from gptme_coordination.db import CoordinationDB
    from gptme_coordination.work import WorkClaimManager
    from gptme_coordination.worktree_guard import run_push_guard

    db_path = tmp_path / "coord.db"
    with CoordinationDB(db_path) as db:
        assert WorkClaimManager(db).claim(
            "other-agent", "pr-branch:feat", ttl_minutes=60
        )

    rc = run_push_guard(
        ["refs/heads/feat abc123 refs/heads/feat def456"],
        worktree_root=tmp_path / "wt",
        brain_root=tmp_path / "brain",
        remote_url="git@github.com:org/repo.git",
        session_id="sess-1",
        agent_id="agent-b",
        deny=False,
        db_path=db_path,
    )
    assert rc == 0

    with CoordinationDB(db_path) as db:
        qualified = WorkClaimManager(db).get("pr-branch:org/repo#feat")
    assert qualified is not None, "qualified claim was skipped on legacy alias"
    assert qualified.claimer == "agent-b"
    assert qualified.status == "claimed"


def test_session_pid_reads_agent_neutral_spelling(monkeypatch) -> None:
    """AGENT_SESSION_PID is honored, with BOB_SESSION_PID as legacy alias.

    Regression for gptme/gptme-contrib#1705: a guard that resolves holder
    liveness only from ``BOB_*`` is a silent no-op for every other agent.
    """
    from gptme_coordination.worktree_guard import _get_session_pid

    monkeypatch.delenv("AGENT_SESSION_PID", raising=False)
    monkeypatch.delenv("BOB_SESSION_PID", raising=False)
    assert _get_session_pid() == 0

    monkeypatch.setenv("AGENT_SESSION_PID", "23456")
    assert _get_session_pid() == 23456

    # Legacy spelling still works on its own.
    monkeypatch.delenv("AGENT_SESSION_PID", raising=False)
    monkeypatch.setenv("BOB_SESSION_PID", "12345")
    assert _get_session_pid() == 12345

    # Preferred spelling wins when both are set.
    monkeypatch.setenv("AGENT_SESSION_PID", "23456")
    assert _get_session_pid() == 23456

    # A malformed preferred value must not mask a valid legacy one.
    monkeypatch.setenv("AGENT_SESSION_PID", "not-a-pid")
    assert _get_session_pid() == 12345


def test_agent_id_reads_agent_neutral_spelling(monkeypatch) -> None:
    """AGENT_ID is honored; BOB_AUTONOMOUS_AGENT_ID stays the legacy alias."""
    from gptme_coordination.worktree_guard import (
        _get_agent_id,
        _get_marker_agent_id,
    )

    # Clear every spelling that any accepted neutralisation of this module can
    # read, so the assertion does not depend on the ambient test environment.
    for var in (
        "AGENT_ID",
        "BOB_AUTONOMOUS_AGENT_ID",
        "AGENT_AMBIENT_HARNESS",
        "BOB_AMBIENT_HARNESS",
        "AGENT_SESSION_ID",
        "GIT_COMMITTER_SESSION_ID",
        "BOB_SESSION_ID",
        "CC_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    # No env-provided id: synthesized from a session id, but the marker records
    # only env-provided ids so a dead holder can never look permanently alive.
    monkeypatch.setenv("GIT_COMMITTER_SESSION_ID", "sess-1")
    assert _get_agent_id() == "bob-autonomous-agent-sess-1"
    assert _get_marker_agent_id() == ""

    monkeypatch.setenv("BOB_AUTONOMOUS_AGENT_ID", "legacy-agent")
    assert _get_agent_id() == "legacy-agent"
    assert _get_marker_agent_id() == "legacy-agent"

    monkeypatch.setenv("AGENT_ID", "neutral-agent")
    assert _get_agent_id() == "neutral-agent"
    assert _get_marker_agent_id() == "neutral-agent"


def test_env_flag_accepts_agent_neutral_and_legacy_spellings(monkeypatch) -> None:
    """Both guard flags accept either env spelling."""
    from gptme_coordination.worktree_guard import (
        _GUARD_FORCE_ENV,
        _PUSH_GUARD_DENY_ENV,
        _env_flag,
    )

    for names in (_GUARD_FORCE_ENV, _PUSH_GUARD_DENY_ENV):
        for name in names:
            for other in names:
                monkeypatch.delenv(other, raising=False)
            monkeypatch.setenv(name, "1")
            assert _env_flag(*names) is True, (names, name)
            monkeypatch.setenv(name, "0")
            assert _env_flag(*names) is False, (names, name)


def test_push_guard_deny_via_agent_neutral_flag(tmp_path: Path, monkeypatch) -> None:
    """AGENT_WORKTREE_PUSH_GUARD_DENY=1 drives the Phase-3 deny path.

    Fails on pre-fix code: the flag was read only from the BOB_* spelling, so
    the guard warned instead of denying.
    """
    from gptme_coordination.db import CoordinationDB
    from gptme_coordination.work import WorkClaimManager
    from gptme_coordination.worktree_guard import run_push_guard

    db_path = tmp_path / "coord.db"
    with CoordinationDB(db_path) as db:
        assert WorkClaimManager(db).claim(
            "holder-agent", "pr-branch:org/repo#feat", ttl_minutes=60
        )

    monkeypatch.delenv("BOB_WORKTREE_PUSH_GUARD_DENY", raising=False)
    monkeypatch.delenv("AGENT_WORKTREE_PUSH_GUARD_DENY", raising=False)

    kwargs = {
        "worktree_root": tmp_path / "wt",
        "brain_root": tmp_path / "brain",
        "remote_url": "git@github.com:org/repo.git",
        "session_id": "sess-1",
        "agent_id": "agent-b",
        "db_path": db_path,
    }
    refspec = ["refs/heads/feat abc123 refs/heads/feat def456"]

    # Warn mode by default.
    assert run_push_guard(refspec, **kwargs) == 0

    # Neutral spelling is enough to block.
    monkeypatch.setenv("AGENT_WORKTREE_PUSH_GUARD_DENY", "1")
    assert run_push_guard(refspec, **kwargs) == 1

    # Legacy spelling still blocks too.
    monkeypatch.delenv("AGENT_WORKTREE_PUSH_GUARD_DENY", raising=False)
    monkeypatch.setenv("BOB_WORKTREE_PUSH_GUARD_DENY", "1")
    assert run_push_guard(refspec, **kwargs) == 1

    rows = [
        json.loads(line)
        for line in (tmp_path / "brain/state/coordination/worktree-guard.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [row["type"] for row in rows] == [
        "push_would_deny",
        "push_deny",
        "push_deny",
    ]


def test_guard_force_via_agent_neutral_flag(tmp_path: Path, monkeypatch) -> None:
    """AGENT_WORKTREE_GUARD_FORCE=1 takes over from a live holder.

    Fails on pre-fix code: the force flag was read only from BOB_*, so an
    otherwise-live holder produced ``would_deny`` instead of ``force``.
    """
    from gptme_coordination.worktree_guard import run_guard

    worktree = tmp_path / "worktrees" / "pr"
    git_dir = worktree / "git"
    git_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "gptme_coordination.worktree_guard._WORKTREE_PREFIX",
        f"{tmp_path}/worktrees/",
    )
    monkeypatch.delenv("BOB_WORKTREE_GUARD_FORCE", raising=False)
    monkeypatch.delenv("AGENT_WORKTREE_GUARD_FORCE", raising=False)

    common = {
        "worktree_root": worktree,
        "git_dir": git_dir,
        "brain_root": tmp_path,
        # A live PID: the holder cannot be reaped, so only force can take over.
        "pid": os.getpid(),
    }
    assert (
        run_guard(session_id="holder-session", agent_id="holder-agent", **common) == 0
    )
    assert run_guard(session_id="my-session", agent_id="my-agent", **common) == 0

    monkeypatch.setenv("AGENT_WORKTREE_GUARD_FORCE", "1")
    assert run_guard(session_id="my-session", agent_id="my-agent", **common) == 0

    rows = [
        json.loads(line)
        for line in (tmp_path / "state/coordination/worktree-guard.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [row["type"] for row in rows] == ["adopt", "would_deny", "force"]
