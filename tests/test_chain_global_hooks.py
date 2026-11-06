"""chain-global-hooks.sh: wrap a local hooksPath so global hooks still run."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

CHAIN = Path(__file__).resolve().parents[1] / "dotfiles" / "chain-global-hooks.sh"


def _run(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(CHAIN), *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env or os.environ.copy(),
    )


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_wrapper(
    path: Path, arg: str, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(path), arg],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )


def test_chain_script_is_executable() -> None:
    assert CHAIN.is_file()
    assert CHAIN.stat().st_mode & stat.S_IXUSR


def test_wraps_global_hook_when_local_dir_is_empty(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho global >>"$1"\n')

    result = _run(["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)])
    assert result.returncode == 0, result.stderr
    wrapper = local_dir / "prepare-commit-msg"
    assert wrapper.is_file()
    assert wrapper.stat().st_mode & stat.S_IXUSR

    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: x\n")
    ran = _run_wrapper(wrapper, str(msg))
    assert ran.returncode == 0
    assert "global" in msg.read_text()


def test_runs_global_then_original_local_hook(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho global >>"$1"\n')
    _write_exec(local_dir / "prepare-commit-msg", '#!/bin/bash\necho local >>"$1"\n')

    result = _run(["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)])
    assert result.returncode == 0, result.stderr
    assert (local_dir / "prepare-commit-msg.local").is_file()

    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: x\n")
    _run_wrapper(local_dir / "prepare-commit-msg", str(msg))
    body = msg.read_text()
    assert body.index("global") < body.index("local")


def test_rerun_does_not_stack_wrappers(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho global >>"$1"\n')
    _write_exec(local_dir / "prepare-commit-msg", '#!/bin/bash\necho local >>"$1"\n')

    args = ["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)]
    assert _run(args).returncode == 0
    assert _run(args).returncode == 0
    assert not (local_dir / "prepare-commit-msg.local.local").exists()
    local_body = (local_dir / "prepare-commit-msg.local").read_text()
    assert "echo local" in local_body
    assert "chain-global-hooks wrapper" not in local_body


def test_symlink_of_same_dir_is_not_wrapped(tmp_path: Path) -> None:
    real = tmp_path / "hooks"
    real.mkdir()
    _write_exec(real / "prepare-commit-msg", '#!/bin/bash\necho original >>"$1"\n')
    link = tmp_path / "link-to-hooks"
    link.symlink_to(real)

    result = _run(["--hooks-dir", str(real), "--global-dir", str(link)])
    assert result.returncode == 0, result.stderr
    assert "already the global dir" in result.stderr
    assert "chain-global-hooks wrapper" not in (real / "prepare-commit-msg").read_text()


def test_rerun_after_local_overwrite_keeps_new_hook(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho global >>"$1"\n')
    _write_exec(local_dir / "prepare-commit-msg", '#!/bin/bash\necho local >>"$1"\n')
    args = ["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)]
    assert _run(args).returncode == 0

    _write_exec(
        local_dir / "prepare-commit-msg",
        '#!/bin/bash\necho prek >>"$1"\n',
    )
    result = _run(args)
    assert result.returncode == 0, result.stderr
    assert "echo prek" in (local_dir / "prepare-commit-msg.local").read_text()
    assert "echo local" not in (local_dir / "prepare-commit-msg.local").read_text()

    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: x\n")
    _run_wrapper(local_dir / "prepare-commit-msg", str(msg))
    body = msg.read_text()
    assert "global" in body
    assert "prek" in body


def test_wrapper_replays_stdin_to_both_hooks(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    _write_exec(global_dir / "pre-push", '#!/bin/bash\ncat >>"$1"\n')
    _write_exec(local_dir / "pre-push", '#!/bin/bash\ncat >>"$1"\n')
    result = _run(["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)])
    assert result.returncode == 0, result.stderr

    log = tmp_path / "stdin.log"
    log.write_text("start\n")
    ran = _run_wrapper(
        local_dir / "pre-push",
        str(log),
        stdin="refs/heads/x abc refs/heads/x def\n",
    )
    assert ran.returncode == 0
    replayed = [
        line for line in log.read_text().splitlines() if line.startswith("refs/")
    ]
    assert replayed == [
        "refs/heads/x abc refs/heads/x def",
        "refs/heads/x abc refs/heads/x def",
    ]


def test_no_local_hooks_path_is_a_noop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    result = _run([str(repo)], env=env)
    assert result.returncode == 0, result.stderr
    assert "no repo-local core.hooksPath" in result.stderr


def test_missing_relative_hooks_dir_is_resolved_from_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", ".hooks"],
        env=env,
        check=True,
    )
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho global >>"$1"\n')
    result = _run([str(repo), "--global-dir", str(global_dir)], env=env)
    assert result.returncode == 0, result.stderr
    assert (repo / ".hooks/prepare-commit-msg").is_file()
    assert not (repo / ".git/.hooks").exists()


def test_commit_wrapper_does_not_wait_for_stdin(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho global >>"$1"\n')
    assert (
        _run(
            ["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)]
        ).returncode
        == 0
    )
    msg = tmp_path / "message"
    msg.write_text("feat: stdin is not a commit hook protocol\n")
    process = subprocess.Popen(
        [str(local_dir / "prepare-commit-msg"), str(msg)], stdin=subprocess.PIPE
    )
    try:
        assert process.wait(timeout=2) == 0
    finally:
        if process.stdin:
            process.stdin.close()
        if process.poll() is None:
            process.kill()
        process.wait()


@pytest.mark.parametrize("manager", ["prek", "pre-commit", "custom"])
def test_chained_precommit_runner_runs_once_and_keeps_guards(
    tmp_path: Path, manager: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    env["ALLOW_GIT_IDENTITY"] = "1"
    subprocess.run(
        ["git", "init", "-q", "-b", "feature", str(repo)], env=env, check=True
    )
    for key, value in [("user.name", "Test"), ("user.email", "test@test.com")]:
        subprocess.run(
            ["git", "-C", str(repo), "config", key, value], env=env, check=True
        )
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".pre-commit-config.yaml"], env=env, check=True
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "calls"
    _write_exec(binary / "prek", f'#!/bin/bash\necho run >>"{log}"\n')
    env["PATH"] = f"{binary}:{env['PATH']}"
    local_dir = repo / ".hooks"
    local_dir.mkdir()
    _write_exec(
        local_dir / "pre-commit",
        f"#!/bin/bash\n# File generated by {manager}\nprek run --files .pre-commit-config.yaml\n",
    )
    # Installed global hooks are symlinked, and their adjacent allowlist must
    # remain the installing agent's config, not the contrib source directory.
    global_dir = tmp_path / "installed/git/hooks"
    global_dir.parent.mkdir(parents=True)
    global_dir.symlink_to(CHAIN.parent / ".config/git/hooks")
    (global_dir.parent / "allowed-identities.conf").write_text(
        'IDENTITY_ALLOWLIST=("test@test.com")\n'
    )
    env.pop("ALLOW_GIT_IDENTITY")
    assert (
        _run(
            ["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)], env=env
        ).returncode
        == 0
    )
    result = subprocess.run(
        [str(local_dir / "pre-commit")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = ["run", "run"] if manager == "custom" else ["run"]
    assert log.read_text().splitlines() == expected
    # Identity guard still runs before delegating the suite to the local runner.
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "unknown@example.com"],
        env=env,
        check=True,
    )
    blocked = subprocess.run(
        [str(local_dir / "pre-commit")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert blocked.returncode != 0
    assert log.read_text().splitlines() == expected


@pytest.mark.parametrize("bare", [False, True])
def test_relative_hooks_path_uses_git_hook_cwd(tmp_path: Path, bare: bool) -> None:
    repo = tmp_path / "repo"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    subprocess.run(
        ["git", "init", "-q", *(["--bare"] if bare else []), str(repo)],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", ".hooks"],
        env=env,
        check=True,
    )
    invocation_dir = repo if bare else repo / "nested"
    invocation_dir.mkdir(exist_ok=True)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho ran >>"$1"\n')
    result = _run([str(invocation_dir), "--global-dir", str(global_dir)], env=env)
    assert result.returncode == 0, result.stderr
    message = tmp_path / "message"
    message.write_text("feat: hook cwd\n")
    ran = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "hook",
            "run",
            "prepare-commit-msg",
            "--",
            str(message),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr
    assert message.read_text().endswith("ran\n")
