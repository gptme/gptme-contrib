"""chain-global-hooks.sh: wrap a local hooksPath so global hooks still run."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

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
    ran = subprocess.run(
        [str(wrapper), str(msg)], check=True, capture_output=True, text=True
    )
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
    subprocess.run(
        [str(local_dir / "prepare-commit-msg"), str(msg)],
        check=True,
        capture_output=True,
        text=True,
    )
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
