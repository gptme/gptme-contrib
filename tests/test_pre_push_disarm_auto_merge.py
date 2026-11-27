"""A plain ``git push`` to a gptme/gptme PR branch disarms auto-merge first."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "dotfiles" / ".config" / "git" / "hooks" / "pre-push"
SCRIPT = ROOT / "scripts" / "hooks" / "disarm_auto_merge.py"
FORK = "git@github.com:SomeAgent/gptme.git"
ZERO = "0" * 40
SHA = "a" * 40
LINE = f"refs/heads/fix/x {SHA} refs/heads/fix/x {ZERO}\n"


def _fake_gh(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$GH_LOG"
case "$1" in
    api) printf '%s' "$PR_NUMBERS"; exit "${LOOKUP_RC:-0}" ;;
    pr)
        case "$2" in
            view)
                if [ -n "$AUTO_MERGE" ]; then printf '%s' "$AUTO_MERGE"; else printf '%s' '{"enabledBy":{"login":"someone"}}'; fi
                exit "${VIEW_RC:-0}"
                ;;
            merge) exit "${DISABLE_RC:-0}" ;;
        esac
        ;;
esac
"""
    )
    gh.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_LOG": str(tmp_path / "gh.log"),
        "PR_NUMBERS": "42\n",
    }


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh.log"
    return log.read_text().splitlines() if log.exists() else []


def _run(env: dict[str, str], url: str = FORK, stdin: str = LINE):
    return subprocess.run(
        [str(SCRIPT), "origin", url],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "url",
    [
        FORK,
        "https://github.com/gptme/gptme",
        "ssh://git@github.com/gptme/gptme.git",
    ],
)
def test_open_pr_is_disarmed(tmp_path: Path, url: str) -> None:
    result = _run(_fake_gh(tmp_path), url)
    assert result.returncode == 0, result.stderr
    lookup, view, disable = _calls(tmp_path)
    assert ":fix/x" in lookup
    assert (
        view
        == "pr view 42 --repo gptme/gptme --json autoMergeRequest --jq .autoMergeRequest"
    )
    assert disable == "pr merge 42 --repo gptme/gptme --disable-auto"


def test_unarmed_pr_passes_without_a_disable(tmp_path: Path) -> None:
    """An unarmed PR (e.g. a draft) must not fail `--disable-auto` and refuse the push."""
    result = _run({**_fake_gh(tmp_path), "AUTO_MERGE": "null"})
    assert result.returncode == 0, result.stderr
    assert not any("--disable-auto" in c for c in _calls(tmp_path))


def test_branch_without_an_open_pr_passes(tmp_path: Path) -> None:
    env = {**_fake_gh(tmp_path), "PR_NUMBERS": ""}
    assert _run(env).returncode == 0
    assert len(_calls(tmp_path)) == 1


@pytest.mark.parametrize(
    ("url", "stdin"),
    [
        ("git@github.com:gptme/gptme-contrib.git", LINE),
        ("/tmp/local/gptme", LINE),
        (FORK, f"refs/heads/master {SHA} refs/heads/master {SHA}\n"),
        (FORK, f"(delete) {ZERO} refs/heads/fix/x {SHA}\n"),
        (FORK, f"refs/tags/v1 {SHA} refs/tags/v1 {ZERO}\n"),
    ],
)
def test_other_pushes_pass_untouched(tmp_path: Path, url: str, stdin: str) -> None:
    env = {**_fake_gh(tmp_path), "LOOKUP_RC": "1", "DISABLE_RC": "1"}
    assert _run(env, url, stdin).returncode == 0
    assert _calls(tmp_path) == []


@pytest.mark.parametrize(("failure", "calls"), [("LOOKUP_RC", 1), ("DISABLE_RC", 4)])
def test_failure_refuses_the_push(tmp_path: Path, failure: str, calls: int) -> None:
    result = _run({**_fake_gh(tmp_path), failure: "1"})
    assert result.returncode == 1
    assert "push refused" in result.stderr
    assert len(_calls(tmp_path)) == calls  # a failed disable is retried once


@pytest.mark.parametrize(("disable_rc", "expected"), [("0", 0), ("1", 1)])
def test_global_hook_runs_the_disarm(
    tmp_path: Path, disable_rc: str, expected: int
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "config", "user.email", "t@t"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    (repo / "f").write_text("x\n")
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "commit", "--no-verify", "-qm", "f"], check=True)
    sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    env = {
        **_fake_gh(tmp_path),
        "DISABLE_RC": disable_rc,
        # Keep the real worktree guard's claims out of the live workspace.
        "COORDINATION_DB": str(tmp_path / "coord.db"),
        "AGENT_WORKSPACE": str(tmp_path),
    }
    result = subprocess.run(
        [str(HOOK), "origin", FORK],
        cwd=repo,
        input=f"refs/heads/fix/x {sha} refs/heads/fix/x {ZERO}\n",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert "pr merge 42 --repo gptme/gptme --disable-auto" in _calls(tmp_path)
