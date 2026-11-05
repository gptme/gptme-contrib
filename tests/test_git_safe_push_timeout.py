"""Regression: a push that never returns must not hold ``.git/commit.lock`` forever.

``git-safe-push-master`` bounded its flock *wait* but imposed no bound on
``git push`` itself. A push that loses its TCP connection mid-transfer sleeps on
a peer that will never answer while still holding fd 9 on ``.git/commit.lock``,
so every cooperating ``git-safe-commit`` in the workspace blocks behind a single
dead network call (observed 2026-11-05: three sessions contended from ~02:15
with zero progress; the holder was a ``git push`` at 8m10s, state ``Sl``, with no
socket).

These tests use a ``git`` shim on PATH that hangs only on ``push`` and delegates
everything else to the real binary, so the wrapper runs its real logic against a
real repository while the transfer never completes.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "git" / "git-safe-push-master"
)

TIMEOUT_BIN = shutil.which("timeout") or shutil.which("gtimeout")
# Resolved once, before the fixture prepends its hanging shim to PATH.
REAL_GIT = shutil.which("git") or "git"

pytestmark = pytest.mark.skipif(
    TIMEOUT_BIN is None, reason="no timeout/gtimeout binary to enforce the bound"
)


def _git(cwd: Path, *args: str, env: dict[str, str]) -> str:
    completed = subprocess.run(
        [REAL_GIT, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return str(completed.stdout)


def _base_env(tmp_path: Path) -> dict[str, str]:
    # Never mutate os.environ: sibling tests and the parent agent share it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="file",
        GIT_SAFE_PUSH_LOCK_TIMEOUT="10",
        GIT_SAFE_PUSH_RETRY_SLEEP="0",
        # The clone's refs/remotes/origin/HEAD may be absent in a fixture; the
        # trunk derivation is covered by test_git_safe_push_trunk.py.
        GIT_SAFE_TRUNK="main",
    )
    config = {
        "user.name": "Push Timeout Test",
        "user.email": "test@example.invalid",
        "core.hooksPath": os.devnull,
        "commit.gpgsign": "false",
        "tag.gpgsign": "false",
        "protocol.allow": "never",
        "protocol.file.allow": "always",
    }
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for i, (key, value) in enumerate(config.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


@pytest.fixture
def repo(tmp_path: Path):
    """Bare ``main`` remote + a clone whose commit is not yet published."""
    env = _base_env(tmp_path)

    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote), env=env)
    _git(tmp_path, "clone", str(remote), str(local), env=env)
    (local / "base.txt").write_text("base\n")
    _git(local, "add", "-A", env=env)
    _git(local, "commit", "-m", "base", env=env)
    _git(local, "push", "-u", "origin", "main", env=env)
    # A second, unpublished local commit so the wrapper has something to publish.
    (local / "local.txt").write_text("local\n")
    _git(local, "add", "-A", env=env)
    _git(local, "commit", "-m", "local work", env=env)

    # PATH shim: hang on `push`, delegate everything else to the real git.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    push_log = tmp_path / "push-invocations.log"
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = "push" ]; then\n'
        '    echo "push" >> "$TEST_PUSH_LOG"\n'
        "    exec sleep 60\n"
        "fi\n"
        f'exec "{REAL_GIT}" "$@"\n'
    )
    shim.chmod(0o755)

    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    env["TEST_PUSH_LOG"] = str(push_log)
    env["GIT_SAFE_PUSH_TIMEOUT"] = "2"
    return {
        "env": env,
        "local": local,
        "remote": remote,
        "push_log": push_log,
    }


def _run(repo, **overrides) -> subprocess.CompletedProcess[str]:
    env = dict(repo["env"])
    env.update(overrides)
    return subprocess.run(
        [str(SCRIPT)],
        cwd=repo["local"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_hung_push_times_out_and_reports_no_progress(repo):
    local_head = _git(repo["local"], "rev-parse", "HEAD", env=repo["env"]).strip()
    remote_head = _git(repo["remote"], "rev-parse", "main", env=repo["env"]).strip()

    result = _run(repo)

    diagnostic = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode != 0, diagnostic
    assert "no progress within 2s" in result.stderr, diagnostic
    assert "Commit(s) remain local" in result.stderr, diagnostic
    assert "was not advanced" in result.stderr, diagnostic
    # Commit(s) and remote are both untouched: a ref update is atomic.
    assert (
        _git(repo["local"], "rev-parse", "HEAD", env=repo["env"]).strip() == local_head
    )
    assert (
        _git(repo["remote"], "rev-parse", "main", env=repo["env"]).strip()
        == remote_head
    )


def test_hung_push_is_not_retried_as_a_remote_race(repo):
    """One dead push must not become three dead pushes (fast-forward + squash)."""
    result = _run(repo)

    assert result.returncode != 0
    invocations = repo["push_log"].read_text().split().count("push")
    assert invocations == 1, f"push ran {invocations} times:\n{result.stderr}"
    assert "remote advanced during" not in result.stderr
    assert "did not advance\n" not in result.stderr


def test_hung_squash_path_push_exits_without_squash_retry_loop(repo):
    """A divergent remote routes through the squash path; the bound still holds."""
    upstream = repo["local"].parent / "upstream"
    _git(
        repo["local"].parent,
        "clone",
        str(repo["remote"]),
        str(upstream),
        env=repo["env"],
    )
    (upstream / "remote.txt").write_text("remote\n")
    _git(upstream, "add", "-A", env=repo["env"])
    _git(upstream, "commit", "-m", "remote work", env=repo["env"])
    _git(upstream, "push", "origin", "main", env=repo["env"])

    result = _run(repo)

    diagnostic = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode != 0, diagnostic
    assert "no progress within 2s" in result.stderr, diagnostic
    assert repo["push_log"].read_text().split().count("push") == 1, diagnostic
    assert (
        _git(repo["remote"], "rev-parse", "main", env=repo["env"]).strip()
        == _git(upstream, "rev-parse", "HEAD", env=repo["env"]).strip()
    )


def test_zero_disables_the_bound(repo):
    """GIT_SAFE_PUSH_TIMEOUT=0 must not wrap the push at all."""
    env = dict(repo["env"])
    env["GIT_SAFE_PUSH_TIMEOUT"] = "0"
    # A shim that would time out at 2s but succeeds immediately when unbounded.
    shim = Path(env["PATH"].split(os.pathsep)[0]) / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = "push" ]; then\n'
        '    echo "push" >> "$TEST_PUSH_LOG"\n'
        "    exit 0\n"
        "fi\n"
        f'exec "{REAL_GIT}" "$@"\n'
    )
    shim.chmod(0o755)

    result = subprocess.run(
        [str(SCRIPT)],
        cwd=repo["local"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    diagnostic = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, diagnostic
    assert "no progress within" not in result.stderr, diagnostic


@pytest.mark.parametrize("value", ["-1", "abc", "1.5"])
def test_invalid_timeout_is_rejected(repo, value):
    result = _run(repo, GIT_SAFE_PUSH_TIMEOUT=value)

    assert result.returncode == 2
    assert "GIT_SAFE_PUSH_TIMEOUT must be a non-negative integer" in result.stderr
    assert not repo["push_log"].exists()
