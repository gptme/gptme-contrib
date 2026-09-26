#!/usr/bin/env python3
"""Integration tests for dotfiles/install.sh.

Covers the forked-agent path: install.sh must write
``~/.config/git/allowed-identities.conf`` from the installing agent's git
identity, otherwise the global pre-commit hook refuses every commit
(gptme/gptme-contrib#1705, item 1).

Run with: pytest tests/integration/test_dotfiles_install.py -v
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

DOTFILES_DIR = Path(__file__).parent.parent.parent / "dotfiles"
INSTALL_SH = DOTFILES_DIR / "install.sh"


def _clean_git_env() -> dict:
    """Strip every GIT_* var so install.sh cannot touch the host repo config."""
    import os

    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    return env


@pytest.fixture
def fake_home():
    """A throwaway HOME with a configured git identity."""
    home = Path(tempfile.mkdtemp(prefix="dotfiles_install_test_"))
    yield home
    shutil.rmtree(home, ignore_errors=True)


def _set_identity(home: Path, email: str) -> dict:
    env = _clean_git_env()
    env["HOME"] = str(home)
    env["DOTFILES_FORCE"] = "1"
    env.pop("XDG_CONFIG_HOME", None)
    for key, value in (("user.email", email), ("user.name", "Test Agent")):
        subprocess.run(
            ["git", "config", "--global", key, value],
            env=env,
            check=True,
            capture_output=True,
        )
    return env


def _run_install(env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    # Run from a non-repo cwd by default so the test cannot pick up the host
    # checkout's repo-local identity.
    return subprocess.run(
        ["bash", str(INSTALL_SH)],
        env=env,
        cwd=str(cwd or env["HOME"]),
        capture_output=True,
        text=True,
    )


def test_writes_allowed_identities_conf_for_forked_agent(fake_home):
    """A non-Bob identity means the hook would refuse every commit without this file."""
    env = _set_identity(fake_home, "agent@example.com")
    result = _run_install(env)
    assert result.returncode == 0, result.stderr

    conf = fake_home / ".config" / "git" / "allowed-identities.conf"
    assert conf.exists(), result.stdout
    assert "agent@example.com" in conf.read_text()


def test_builtin_identity_needs_no_conf(fake_home):
    """Bob's identities are the hook's built-in defaults; no file should appear."""
    env = _set_identity(fake_home, "bob@superuserlabs.org")
    result = _run_install(env)
    assert result.returncode == 0, result.stderr

    conf = fake_home / ".config" / "git" / "allowed-identities.conf"
    assert not conf.exists(), result.stdout


def test_existing_conf_is_not_overwritten(fake_home):
    """A hand-curated allowlist must survive a re-run."""
    env = _set_identity(fake_home, "agent@example.com")
    conf = fake_home / ".config" / "git" / "allowed-identities.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text('IDENTITY_ALLOWLIST=(\n    "curated@example.com"\n)\n')

    result = _run_install(env)
    assert result.returncode == 0, result.stderr
    assert "curated@example.com" in conf.read_text()
    assert "agent@example.com" not in conf.read_text()


def test_quotes_identity_so_it_cannot_inject_shell(fake_home):
    """The conf is shell-sourced: a crafted user.email must stay inert."""
    nasty = 'x"; touch "$HOME/pwned"; echo "'
    env = _set_identity(fake_home, nasty)
    result = _run_install(env)
    assert result.returncode == 0, result.stderr

    conf = fake_home / ".config" / "git" / "allowed-identities.conf"
    assert conf.exists(), result.stdout

    source_env = _clean_git_env()
    source_env["HOME"] = str(fake_home)
    sourced = subprocess.run(
        ["bash", "-c", f'source "{conf}"; printf "%s" "${{IDENTITY_ALLOWLIST[0]}}"'],
        env=source_env,
        capture_output=True,
        text=True,
    )
    assert sourced.returncode == 0, sourced.stderr
    assert sourced.stdout == nasty
    assert not (fake_home / "pwned").exists()


def test_missing_identity_does_not_fail_install(fake_home):
    """No git identity -> warn, skip the conf, still install the hooks."""
    env = _clean_git_env()
    env["HOME"] = str(fake_home)
    env["DOTFILES_FORCE"] = "1"
    env.pop("XDG_CONFIG_HOME", None)

    result = _run_install(env)
    assert result.returncode == 0, result.stderr
    assert not (fake_home / ".config" / "git" / "allowed-identities.conf").exists()


def test_repo_local_identity_is_never_used_for_the_conf(fake_home, tmp_path):
    """The cwd's repo-local identity is not the agent's commit identity.

    install.sh is normally run from the dotfiles checkout (see README), so
    falling back to ``git config user.email`` would read that checkout's config.
    A conf naming the wrong identity blocks every commit, which is worse than
    writing no conf at all — so the fallback must not exist.
    """
    env = _clean_git_env()
    env["HOME"] = str(fake_home)
    env["DOTFILES_FORCE"] = "1"
    env.pop("XDG_CONFIG_HOME", None)

    # A checkout with a repo-local identity, and no global identity at all.
    checkout = tmp_path / "dotfiles-checkout"
    checkout.mkdir()
    for args in (
        ["git", "init"],
        ["git", "config", "--local", "user.email", "checkout-owner@example.com"],
        ["git", "config", "--local", "user.name", "Checkout Owner"],
    ):
        subprocess.run(args, cwd=checkout, env=env, check=True, capture_output=True)

    result = _run_install(env, cwd=checkout)
    assert result.returncode == 0, result.stderr

    conf = fake_home / ".config" / "git" / "allowed-identities.conf"
    assert not conf.exists(), (
        "repo-local identity leaked into allowed-identities.conf: "
        f"{conf.read_text() if conf.exists() else ''}"
    )
    assert "not set" in result.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
