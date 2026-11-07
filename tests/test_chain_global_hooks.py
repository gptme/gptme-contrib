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
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HOME"] = str(tmp_path / "home")
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    Path(env["HOME"]).mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, env=env, check=True, capture_output=True
    )
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
    # Receive-side hooks have a separate Git-directory-relative directory.
    assert not (repo / ".git/.hooks/prepare-commit-msg").exists()


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
@pytest.mark.parametrize("runner_available", [False, True])
@pytest.mark.parametrize("relative_global", [False, True])
def test_chained_precommit_runner_runs_once_and_keeps_guards(
    tmp_path: Path, manager: str, runner_available: bool, relative_global: bool
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
    _write_exec(
        binary / "prek",
        f'#!/bin/bash\nprintf "%s\\n" "$*" >>"{log}"\n'
        '[[ "$1 $2" == "run --files" ]]\n',
    )
    env["PATH"] = f"{binary}:{env['PATH']}"
    unstaged = repo / "concurrent.txt"
    unstaged.write_text("another session's unstaged work\n")
    local_dir = repo / ".hooks"
    local_dir.mkdir()
    _write_exec(
        local_dir / "pre-commit",
        f"#!/bin/bash\n# File generated by {manager}\n"
        + (
            "prek run --files .pre-commit-config.yaml\n"
            if manager == "custom"
            else f"{manager} hook-impl --config=.pre-commit-config.yaml --hook-type pre-commit\n"
        ),
    )
    # Installed global hooks are symlinked, and their adjacent allowlist must
    # remain the installing agent's config, not the contrib source directory.
    global_dir = (repo if relative_global else tmp_path) / "installed/git/hooks"
    global_dir.parent.mkdir(parents=True)
    global_dir.symlink_to(CHAIN.parent / ".config/git/hooks")
    (global_dir.parent / "allowed-identities.conf").write_text(
        'IDENTITY_ALLOWLIST=("test@test.com")\n'
    )
    env.pop("ALLOW_GIT_IDENTITY")
    args = ["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)]
    if relative_global:
        subprocess.run(
            ["git", "config", "--global", "core.hooksPath", "installed/git/hooks"],
            env=env,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", ".hooks"],
            env=env,
            check=True,
        )
        args = [str(repo)]
    assert _run(args, env=env).returncode == 0
    if not runner_available:
        import shutil

        # Keep shell/git utilities but hide both managers. The saved generated
        # runner could still work by absolute path; never skip it as success.
        for name in ["bash", "dirname", "git", "grep", "wc"]:
            executable = shutil.which(name)
            assert executable is not None
            (binary / name).symlink_to(executable)
        (binary / "prek").unlink()
        env["PATH"] = str(binary)
    result = subprocess.run(
        [str(local_dir / "pre-commit")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if not runner_available:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not log.exists()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    expected = ["run --files .pre-commit-config.yaml"] * (
        2 if manager == "custom" else 1
    )
    assert log.read_text().splitlines() == expected
    assert unstaged.read_text() == "another session's unstaged work\n"
    # Identity guard still runs before executing the suite.
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


@pytest.mark.parametrize("tilde_scope", ["local", "global"])
def test_configured_tilde_hooks_paths_are_expanded(
    tmp_path: Path, tilde_scope: str
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    home = tmp_path / "home"
    home.mkdir()
    env["HOME"] = str(home)
    env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    local_dir = home / "local-hooks"
    global_dir = home / "global-hooks"
    global_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho ran >>"$1"\n')
    for scope, value in [
        ("--local", "~/local-hooks" if tilde_scope == "local" else str(local_dir)),
        ("--global", "~/global-hooks" if tilde_scope == "global" else str(global_dir)),
    ]:
        subprocess.run(
            ["git", "-C", str(repo), "config", scope, "core.hooksPath", value],
            env=env,
            check=True,
        )
    result = _run([str(repo)], env=env)
    assert result.returncode == 0, result.stderr
    assert (local_dir / "prepare-commit-msg").is_file()
    message = tmp_path / "message"
    message.write_text("feat: tilde path\n")
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
    assert not (home / "~").exists()
    assert not (repo / "~").exists()


@pytest.mark.parametrize("global_present", [False, True])
def test_generated_local_runner_is_preserved_without_global_check_owner(
    tmp_path: Path, global_present: bool
) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    log = tmp_path / "calls"
    if global_present:
        _write_exec(global_dir / "pre-commit", f'#!/bin/bash\necho global >>"{log}"\n')
    _write_exec(
        local_dir / "pre-commit",
        f'#!/bin/bash\n# File generated by prek\necho local >>"{log}"\n',
    )
    installed = _run(["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)])
    assert installed.returncode == 0, installed.stderr
    result = _run_wrapper(local_dir / "pre-commit", "")
    assert result.returncode == 0
    assert log.read_text().splitlines() == (
        ["global", "local"] if global_present else ["local"]
    )


@pytest.mark.parametrize(
    "comment",
    [
        "",
        "\n# --config=.pre-commit-config.yaml\n",
        " --config=.pre-commit-config.yaml\n",
    ],
)
@pytest.mark.parametrize("config", ["checks.yaml", ".pre-commit-config.yaml"])
@pytest.mark.parametrize("default_exists", [False, True])
def test_chaining_refuses_generated_runner_without_matching_config(
    tmp_path: Path, config: str, default_exists: bool, comment: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    local_dir = repo / ".hooks"
    local_dir.mkdir()
    if default_exists:
        (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    original = (
        "#!/bin/sh\n# File generated by prek: https://github.com/j178/prek\n"
        f'exec prek hook-impl --config="{config}" --hook-type=pre-commit -- "$@"\n'
        + comment
    )
    _write_exec(local_dir / "pre-commit", original)
    result = _run(
        [
            "--hooks-dir",
            str(local_dir),
            "--global-dir",
            str(CHAIN.parent / ".config/git/hooks"),
        ],
        env=env,
    )
    if (
        default_exists
        and config == ".pre-commit-config.yaml"
        and not comment.startswith(" ")
    ):
        assert result.returncode == 0, result.stderr
        assert (local_dir / "pre-commit.local").read_text() == original
        return
    assert result.returncode != 0
    assert "cannot safely replace" in result.stderr
    assert (local_dir / "pre-commit").read_text() == original
    assert not (local_dir / "pre-commit.local").exists()
    assert not (local_dir / "prepare-commit-msg").exists()


def test_relative_global_hooks_path_uses_repository_root(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", ".hooks"],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "core.hooksPath", ".global-hooks"],
        env=env,
        check=True,
    )
    global_dir = repo / ".global-hooks"
    global_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", '#!/bin/bash\necho ran >>"$1"\n')
    result = _run([str(repo)], env=env)
    assert result.returncode == 0, result.stderr
    assert (repo / ".hooks/prepare-commit-msg").exists()


@pytest.mark.parametrize("explicit_repo", [False, True])
def test_generated_runner_in_git_hooks_requires_explicit_repo(
    tmp_path: Path, explicit_repo: bool
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    hooks = repo / ".git/hooks"
    original = (
        "#!/bin/sh\n# File generated by prek\n"
        'exec prek hook-impl --config=.pre-commit-config.yaml -- "$@"\n'
    )
    _write_exec(hooks / "pre-commit", original)
    result = _run(
        [
            *([str(repo)] if explicit_repo else []),
            "--hooks-dir",
            str(hooks),
            "--global-dir",
            str(CHAIN.parent / ".config/git/hooks"),
        ],
        env=env,
    )
    if explicit_repo:
        assert result.returncode == 0, result.stderr
        assert (hooks / "pre-commit.local").read_text() == original
    else:
        assert result.returncode != 0
        assert "pass REPO explicitly" in result.stderr
        assert (hooks / "pre-commit").read_text() == original
        assert not (hooks / "pre-commit.local").exists()
        assert not (hooks / "prepare-commit-msg").exists()


@pytest.mark.parametrize("formatter", [False, True])
def test_chained_precommit_refuses_partially_staged_file(
    tmp_path: Path, formatter: bool
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    env["ALLOW_GIT_IDENTITY"] = "1"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "feature", str(repo)], env=env, check=True
    )
    for key, value in [("user.name", "Test"), ("user.email", "test@test.com")]:
        subprocess.run(
            ["git", "-C", str(repo), "config", key, value], env=env, check=True
        )
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    target = repo / "partial file.txt"
    target.write_text("staged contents\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], env=env, check=True)
    target.write_text("staged contents\ndeliberately unstaged\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "calls"
    _write_exec(
        binary / "prek",
        f'#!/bin/bash\necho ran >>"{log}"\n'
        + (
            "echo formatted >>'partial file.txt'\nexit 1\n" if formatter else "exit 0\n"
        ),
    )
    env["PATH"] = f"{binary}:{env['PATH']}"
    hooks = repo / ".git/hooks"
    _write_exec(
        hooks / "pre-commit",
        "#!/bin/bash\n# File generated by prek\n"
        "prek hook-impl --config=.pre-commit-config.yaml\n",
    )
    installed = _run(
        [
            str(repo),
            "--hooks-dir",
            str(hooks),
            "--global-dir",
            str(CHAIN.parent / ".config/git/hooks"),
        ],
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    result = subprocess.run(
        [str(hooks / "pre-commit")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "partially staged" in result.stdout + result.stderr
    assert not log.exists()
    staged = subprocess.run(
        ["git", "-C", str(repo), "show", ":partial file.txt"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout == "staged contents\n"
    assert target.read_text() == "staged contents\ndeliberately unstaged\n"


@pytest.mark.parametrize("deleted", [False, True])
def test_chained_precommit_refuses_unstaged_config(
    tmp_path: Path, deleted: bool
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    env["ALLOW_GIT_IDENTITY"] = "1"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "feature", str(repo)], env=env, check=True
    )
    config = repo / ".pre-commit-config.yaml"
    original = "repos: []\n# indexed validation policy\n"
    config.write_text(original)
    (repo / "invalid.py").write_text("invalid python !\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], env=env, check=True)
    hooks = repo / ".git/hooks"
    _write_exec(
        hooks / "pre-commit",
        "#!/bin/bash\n# File generated by prek\n"
        "prek hook-impl --config=.pre-commit-config.yaml\n",
    )
    installed = _run(
        [
            str(repo),
            "--hooks-dir",
            str(hooks),
            "--global-dir",
            str(CHAIN.parent / ".config/git/hooks"),
        ],
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    # Remove the config from the staged diff while keeping it in the index.
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@test.com",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "baseline",
        ],
        env=env,
        check=True,
    )
    (repo / "invalid.py").write_text("still invalid python !\n")
    subprocess.run(["git", "-C", str(repo), "add", "invalid.py"], env=env, check=True)
    if deleted:
        config.unlink()
    else:
        config.write_text("repos: []\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "calls"
    _write_exec(binary / "prek", f'#!/bin/bash\necho ran >>"{log}"\nexit 0\n')
    env["PATH"] = f"{binary}:{env['PATH']}"
    before = subprocess.check_output(["git", "-C", str(repo), "write-tree"], env=env)
    result = subprocess.run(
        [str(hooks / "pre-commit")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "unstaged pre-commit configuration" in result.stdout + result.stderr
    assert not log.exists()
    assert (
        subprocess.check_output(["git", "-C", str(repo), "write-tree"], env=env)
        == before
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(repo), "show", ":.pre-commit-config.yaml"],
            env=env,
        ).decode()
        == original
    )
    assert not config.exists() if deleted else config.read_text() == "repos: []\n"


@pytest.mark.parametrize("restaged", [False, True])
def test_submodule_partial_staging_checks_pointer_not_worktree_dirt(
    tmp_path: Path, restaged: bool
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    env["ALLOW_GIT_IDENTITY"] = "1"
    env["AGENT_SESSION_TRAILER"] = "0"

    def git(repo: Path, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], env=env, text=True
        ).strip()

    source = tmp_path / "source"
    repo = tmp_path / "repo"
    for directory in (source, repo):
        directory.mkdir()
        git(directory, "init", "-q", "-b", "feature")
        git(directory, "config", "user.name", "Test")
        git(directory, "config", "user.email", "test@test.com")
    (source / "tracked.txt").write_text("initial\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "initial")
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(source),
        "child",
    )
    git(repo, "commit", "-qm", "add child")
    child = repo / "child"
    (source / "tracked.txt").write_text("next\n")
    git(source, "commit", "-qam", "next")
    git(child, "fetch", "origin")
    git(child, "checkout", "--detach", "origin/feature")
    git(repo, "add", "child")
    if not restaged:
        # Move the child away from the newly staged pointer.
        git(child, "checkout", "--detach", "HEAD~1")
    (child / "tracked.txt").write_text("unfinished child edits\n")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    git(repo, "add", ".pre-commit-config.yaml")
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "calls"
    _write_exec(binary / "prek", f'#!/bin/bash\necho ran >>"{log}"\n')
    env["PATH"] = f"{binary}:{env['PATH']}"
    before = git(repo, "ls-files", "--stage")
    result = subprocess.run(
        [str(CHAIN.parent / ".config/git/hooks/pre-commit")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if restaged:
        assert result.returncode == 0, result.stdout + result.stderr
        assert log.read_text() == "ran\n"
    else:
        # Ignoring child dirt must not ignore a different checked-out commit.
        assert result.returncode != 0, result.stdout + result.stderr
        assert not log.exists()
    assert git(repo, "ls-files", "--stage") == before
    assert (child / "tracked.txt").read_text() == "unfinished child edits\n"


def test_dual_proc_receive_is_refused_before_any_hook_is_rewritten(
    tmp_path: Path,
) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", "#!/bin/bash\nexit 0\n")
    for directory in (global_dir, local_dir):
        _write_exec(
            directory / "proc-receive", "#!/bin/bash\nread -r line\necho reply\n"
        )
    original = (local_dir / "proc-receive").read_bytes()
    result = _run(["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)])
    assert result.returncode != 0
    assert "cannot chain two proc-receive" in result.stderr
    assert (local_dir / "proc-receive").read_bytes() == original
    assert not (local_dir / "proc-receive.local").exists()
    assert not (local_dir / "prepare-commit-msg").exists()


@pytest.mark.parametrize("owner", ["global", "local"])
def test_single_proc_receive_keeps_live_stdin_and_refuses_later_dual_handlers(
    tmp_path: Path, owner: str
) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    handler = (global_dir if owner == "global" else local_dir) / "proc-receive"
    _write_exec(handler, "#!/bin/bash\nread -r line\nprintf 'reply:%s\\n' \"$line\"\n")
    args = ["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)]
    assert _run(args).returncode == 0
    process = subprocess.Popen(
        [str(local_dir / "proc-receive")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdin is not None
        process.stdin.write("negotiate\n")
        process.stdin.flush()
        assert process.wait(timeout=2) == 0
        assert process.stdout is not None
        assert process.stdout.read() == "reply:negotiate\n"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
    second = (
        local_dir / "proc-receive.local"
        if owner == "global"
        else global_dir / "proc-receive"
    )
    _write_exec(second, "#!/bin/bash\necho unexpected\n")
    before = (local_dir / "proc-receive").read_bytes()
    assert _run(args).returncode != 0
    assert (local_dir / "proc-receive").read_bytes() == before
    result = subprocess.run(
        [str(local_dir / "proc-receive")],
        input="negotiate\n",
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert result.returncode != 0
    assert "cannot chain two proc-receive" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("manager", ["prek", "pre-commit"])
@pytest.mark.parametrize("changed", [False, True])
def test_saved_generated_runner_must_match_validated_installation(
    tmp_path: Path, manager: str, changed: bool
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    hooks = repo / ".hooks"
    hooks.mkdir()
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    log = tmp_path / "calls"
    _write_exec(
        global_dir / "pre-commit",
        "#!/bin/bash\n# gptme-contrib: owns-generated-precommit-checks\n"
        f'echo global >>"{log}"\n',
    )
    original = (
        f"#!/bin/bash\n# File generated by {manager}\n"
        f"{manager} hook-impl --config=.pre-commit-config.yaml\n"
    )
    _write_exec(hooks / "pre-commit", original)
    result = _run(
        [str(repo), "--hooks-dir", str(hooks), "--global-dir", str(global_dir)],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    binary = tmp_path / "bin"
    binary.mkdir()
    _write_exec(binary / "prek", "#!/bin/bash\nexit 0\n")
    env["PATH"] = f"{binary}:{env['PATH']}"
    if changed:
        (hooks / "pre-commit.local").write_text(
            original.replace("--config=.pre-commit-config.yaml", "--config=checks.yaml")
        )
        (repo / "checks.yaml").write_text("repos: []\n")
    result = subprocess.run(
        [str(hooks / "pre-commit")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if changed:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "saved generated pre-commit runner changed" in result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert log.read_text() == "global\n"


@pytest.mark.parametrize(
    "extra",
    [
        "\n./validate-secrets.sh\n",
        "; ./validate-secrets.sh\n",
        " && ./validate-secrets.sh\n",
        " | ./validate-secrets.sh\n",
        "\ntrap './validate-secrets.sh' EXIT\n",
        "\nPOLICY=$(./validate-secrets.sh)\n",
    ],
)
def test_customized_generated_runner_is_refused_before_rewriting(
    tmp_path: Path, extra: str
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    hooks = repo / ".git/hooks"
    original = (
        "#!/bin/sh\n# File generated by prek\n"
        'prek hook-impl --config=.pre-commit-config.yaml -- "$@"' + extra
    )
    _write_exec(hooks / "pre-commit", original)
    result = _run(
        [
            str(repo),
            "--hooks-dir",
            str(hooks),
            "--global-dir",
            str(CHAIN.parent / ".config/git/hooks"),
        ],
        env=env,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "cannot safely replace" in result.stderr
    assert (hooks / "pre-commit").read_text() == original
    assert not (hooks / "pre-commit.local").exists()
    assert not (hooks / "prepare-commit-msg").exists()


@pytest.mark.parametrize("relative_global", [False, True])
def test_relative_receive_hooks_run_on_real_nonbare_push(
    tmp_path: Path, relative_global: bool
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")

    def git(repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout

    target = tmp_path / "target"
    source = tmp_path / "source"
    for repo in (target, source):
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)], env=env, check=True
        )
    git(target, "config", "core.hooksPath", ".hooks")
    git(target, "config", "receive.denyCurrentBranch", "ignore")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "file").write_text("payload\n")
    git(source, "add", "file")
    git(source, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "initial")
    global_dir = (
        target / ".git/.global-hooks" if relative_global else tmp_path / "global"
    )
    global_dir.mkdir()
    if relative_global:
        git(target, "config", "--global", "core.hooksPath", ".global-hooks")
    local_dir = target / ".git/.hooks"
    local_dir.mkdir()
    log = tmp_path / "calls"
    refs = tmp_path / "refs"
    for owner, directory in (("global", global_dir), ("local", local_dir)):
        _write_exec(
            directory / "pre-receive",
            f'#!/bin/sh\necho {owner}:$PWD >>"{log}"\ncat >>"{refs}"\n',
        )
    args = [str(target)]
    if not relative_global:
        args += ["--global-dir", str(global_dir)]
    installed = _run(args, env=env)
    assert installed.returncode == 0, installed.stderr
    git(source, "push", str(target), "HEAD:refs/heads/main")
    assert log.exists(), "receive-pack did not find the installed wrapper"
    assert log.read_text().splitlines() == [
        f"global:{target}/.git",
        f"local:{target}/.git",
    ]
    lines = refs.read_text().splitlines()
    assert len(lines) == 2 and lines[0] == lines[1]


@pytest.mark.parametrize("manager", ["prek", "pre-commit"])
@pytest.mark.parametrize(
    "extra", ["", "\n./validate-secrets.sh\n", "assignment-command"]
)
def test_stock_generated_runner_templates_are_validated_in_full(
    tmp_path: Path, manager: str, extra: str
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    hooks = repo / ".git/hooks"
    # Captured stock prek script-version 4 and pre-commit hook templates.
    # Neither fixture executes its installed executable during validation.
    here = 'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
    if manager == "prek":
        body = (
            here + 'PREK="/path with spaces/prek"\n'
            "# Check the installed executable before the PATH fallback\n"
            'if [ ! -x "$PREK" ]; then\n'
            '    PREK="prek"\nfi\n'
            'exec "$PREK" hook-impl --hook-dir "$HERE" --script-version 4 --hook-type=pre-commit -- "$@"\n'
        )
    else:
        body = (
            "INSTALL_PYTHON='/path with spaces/python3'\n"
            "ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)\n"
            + here
            + 'ARGS+=(--hook-dir "$HERE" -- "$@")\n'
            'if [ -x "$INSTALL_PYTHON" ]; then\n'
            '    exec "$INSTALL_PYTHON" -mpre_commit "${ARGS[@]}"\n'
            "elif command -v pre-commit > /dev/null; then\n"
            '    exec pre-commit "${ARGS[@]}"\nelse\n'
            "    echo '`pre-commit` not found.  Did you forget to activate your virtualenv?' 1>&2\n"
            "    exit 1\nfi\n"
        )
    if extra == "assignment-command":
        body = body.replace(
            'PREK="/path with spaces/prek"', "PREK= ./validate-secrets.sh"
        )
        body = body.replace(
            "INSTALL_PYTHON='/path with spaces/python3'",
            "INSTALL_PYTHON= ./validate-secrets.sh",
        )
    original = f"#!/bin/sh\n# File generated by {manager}\n" + body
    if extra != "assignment-command":
        original += extra
    _write_exec(hooks / "pre-commit", original)
    result = _run(
        [
            str(repo),
            "--hooks-dir",
            str(hooks),
            "--global-dir",
            str(CHAIN.parent / ".config/git/hooks"),
        ],
        env=env,
    )
    if extra:
        assert result.returncode != 0
        assert (hooks / "pre-commit").read_text() == original
        assert not (hooks / "pre-commit.local").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (hooks / "pre-commit.local").read_text() == original


def test_aliased_receive_proc_handler_does_not_block_commit_wrappers(
    tmp_path: Path,
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", ".hooks"],
        env=env,
        check=True,
    )
    global_dir = repo / ".git/.hooks"
    global_dir.mkdir()
    handler = global_dir / "proc-receive"
    _write_exec(handler, "#!/bin/sh\necho protocol-handler\n")
    original = handler.read_bytes()
    _write_exec(global_dir / "prepare-commit-msg", "#!/bin/sh\necho stamped\n")
    result = _run([str(repo), "--global-dir", str(global_dir)], env=env)
    assert result.returncode == 0, result.stderr
    assert handler.read_bytes() == original
    assert not (global_dir / "proc-receive.local").exists()
    assert (repo / ".hooks/prepare-commit-msg").is_file()


def test_refresh_never_truncates_live_wrapper(tmp_path: Path) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    _write_exec(global_dir / "prepare-commit-msg", "#!/bin/sh\nexit 0\n")
    args = ["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)]
    assert _run(args).returncode == 0
    wrapper = local_dir / "prepare-commit-msg"
    reference = tmp_path / "reference"
    reference.write_bytes(wrapper.read_bytes())
    tools = tmp_path / "bin"
    tools.mkdir()
    # Intercept the heredoc writer exactly when a concurrent Git would see it.
    _write_exec(
        tools / "cat",
        '#!/bin/sh\ncmp -s "$LIVE_WRAPPER" "$REFERENCE_WRAPPER" || exit 77\nexec /bin/cat "$@"\n',
    )
    env = dict(
        os.environ,
        PATH=f"{tools}:{os.environ['PATH']}",
        LIVE_WRAPPER=str(wrapper),
        REFERENCE_WRAPPER=str(reference),
    )
    result = _run(args, env=env)
    assert result.returncode == 0, result.stderr
    assert wrapper.read_bytes() == reference.read_bytes()
    assert wrapper.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("owner", ["global", "local", "both"])
def test_fsmonitor_preserves_one_protocol_and_refuses_dual_handlers(
    tmp_path: Path, owner: str
) -> None:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    local_dir.mkdir()
    for who, directory in (("global", global_dir), ("local", local_dir)):
        if owner in (who, "both"):
            _write_exec(
                directory / "fsmonitor-watchman",
                f"#!/bin/sh\nprintf '{who}-token\\0file\\0'\n",
            )
    args = ["--hooks-dir", str(local_dir), "--global-dir", str(global_dir)]
    result = _run(args)
    if owner == "both":
        assert result.returncode != 0
        assert "cannot chain two fsmonitor-watchman" in result.stderr
        assert not (local_dir / "fsmonitor-watchman.local").exists()
        return
    assert result.returncode == 0, result.stderr
    wrapper = local_dir / "fsmonitor-watchman"
    ran = subprocess.run(
        [str(wrapper), "2", "old-token"], capture_output=True, check=True
    )
    assert ran.stdout == f"{owner}-token\0file\0".encode()
    other = (
        local_dir / "fsmonitor-watchman.local"
        if owner == "global"
        else global_dir / "fsmonitor-watchman"
    )
    _write_exec(other, "#!/bin/sh\nprintf 'other-token\\0other-file\\0'\n")
    before = wrapper.read_bytes()
    assert _run(args).returncode != 0
    assert wrapper.read_bytes() == before
    ran = subprocess.run([str(wrapper), "2", "old-token"], capture_output=True)
    assert ran.returncode != 0
    assert ran.stdout == b""
    assert b"cannot chain two fsmonitor-watchman" in ran.stderr
