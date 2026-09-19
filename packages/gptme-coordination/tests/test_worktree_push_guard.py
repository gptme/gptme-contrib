"""Smoke tests for the worktree push guard (contrib port, ErikBjare/alice#83)."""

from __future__ import annotations

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
    assert origin_slug("https://gitlab.com/org/repo.git") is None


def test_pushed_branches_filters_deletes_and_non_heads() -> None:
    lines = [
        "refs/heads/feat abc123 refs/heads/feat def456",
        "refs/heads/gone " + "0" * 40 + " refs/heads/gone def456",  # deletion
        "refs/tags/v1 abc123 refs/tags/v1 def456",  # not a branch
    ]
    assert pushed_branches(lines) == ["feat"]


def test_entry_point_fails_open_without_package() -> None:
    """Guard entry point exits 0 when the coordination package is missing."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        # Hermetic: guard env inputs must not leak in from the test runner.
        "AGENT_WORKSPACE": "",
        "BOB_SESSION_ID": "",
        "GIT_COMMITTER_SESSION_ID": "",
        "BOB_AUTONOMOUS_AGENT_ID": "",
    }
    result = subprocess.run(
        [sys.executable, str(CONTRIB_ROOT / "scripts/hooks/worktree-push-guard")],
        input="refs/heads/feat abc refs/heads/feat def\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


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
    from datetime import UTC, datetime, timedelta

    from gptme_coordination.work import WorkClaim
    from gptme_coordination.worktree_guard import _claim_expired

    now = datetime.now(UTC)

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


def test_dead_pid_without_authoritative_agent_id_is_dead() -> None:
    """A dead PID must not stay 'alive' on a synthesized marker agent id."""
    from gptme_coordination.worktree_guard import _is_holder_alive

    dead_pid = 2**22  # > pid_max on Linux default configurations
    assert _is_holder_alive({"pid": dead_pid, "agent_id": ""}) is False
    assert _is_holder_alive({"pid": dead_pid, "agent_id": "bob-autonomous-x"}) is True


def test_worktree_guard_entry_point_fails_open_without_package() -> None:
    """Post-commit occupancy entry point exits 0 when the package is missing."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        # Hermetic: guard env inputs must not leak in from the test runner.
        "AGENT_WORKSPACE": "",
        "BOB_SESSION_ID": "",
        "GIT_COMMITTER_SESSION_ID": "",
        "BOB_AUTONOMOUS_AGENT_ID": "",
    }
    result = subprocess.run(
        [sys.executable, str(CONTRIB_ROOT / "scripts/hooks/worktree-guard")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


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
