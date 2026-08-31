from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNERS = ROOT / "scripts" / "runs" / "autonomous"


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _copy_runner(
    name: str, workspace: Path, replacements: dict[str, str] | None = None
) -> Path:
    runner = workspace / "scripts" / "runs" / "autonomous" / name
    runner.parent.mkdir(parents=True, exist_ok=True)
    content = (RUNNERS / name).read_text()
    for old, new in (replacements or {}).items():
        assert old in content
        content = content.replace(old, new)
    runner.write_text(content)
    runner.chmod(0o755)
    return runner


def _clean_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    temp_dir = tmp_path / "tmp"
    home.mkdir()
    temp_dir.mkdir()
    env = os.environ.copy()
    env.update({"HOME": str(home), "TMPDIR": str(temp_dir)})
    env.pop("INVOCATION_ID", None)
    return env


def test_gptme_manual_run_allows_unset_invocation_id_under_nounset(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    runner = _copy_runner(
        "autonomous-run.sh",
        workspace,
        {
            'AGENT_NAME="YourAgent"': 'AGENT_NAME="TestAgent"',
            'WORKSPACE="/path/to/your/workspace"': f'WORKSPACE="{workspace}"',
        },
    )
    call_log = tmp_path / "gptme-call.log"
    _write_executable(
        workspace / "bin" / "gptme",
        '#!/bin/sh\ncat "$2" > "$GPTME_CALL_LOG"\n',
    )
    env = _clean_environment(tmp_path)
    env["GPTME_CALL_LOG"] = str(call_log)

    result = subprocess.run(
        ["bash", "-u", str(runner)],
        cwd=workspace,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout
    assert "**Run Type**: Manual" in call_log.read_text()
    assert "Autonomous run completed successfully" in result.stdout


def _prepare_cc_runner(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _copy_runner(
        "autonomous-run-cc.sh",
        workspace,
        {'AGENT_NAME="YourAgent"': 'AGENT_NAME="TestAgent"'},
    )
    _write_executable(
        workspace / "scripts" / "build-system-prompt.sh",
        '#!/bin/sh\nprintf "test system prompt\\n"\n',
    )
    _write_executable(
        workspace / "bin" / "git",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$GIT_CALL_LOG"\n',
    )
    _write_executable(
        workspace / "bin" / "claude",
        """#!/bin/sh
if [ "${CLAUDE_MODE:-exit}" = "hold" ]; then
    : > "$CLAUDE_STARTED_FILE"
    while [ ! -f "$CLAUDE_RELEASE_FILE" ]; do
        sleep 0.02
    done
elif [ "${CLAUDE_MODE:-exit}" = "spin" ]; then
    while :; do :; done
fi
exit "${CLAUDE_EXIT_CODE:-0}"
""",
    )
    env = _clean_environment(tmp_path)
    git_log = tmp_path / "git-call.log"
    env["GIT_CALL_LOG"] = str(git_log)
    return runner, git_log, env


@pytest.mark.parametrize(
    ("mode", "claude_exit", "runner_args", "expected_exit", "expected_log"),
    [
        ("exit", "17", [], 17, "finished (exit: 17)"),
        ("spin", "0", ["--timeout", "0.05"], 124, "timed out after 0.05s"),
    ],
)
def test_cc_failure_paths_still_push_and_log_completion(
    tmp_path: Path,
    mode: str,
    claude_exit: str,
    runner_args: list[str],
    expected_exit: int,
    expected_log: str,
) -> None:
    runner, git_log, env = _prepare_cc_runner(tmp_path)
    env.update({"CLAUDE_MODE": mode, "CLAUDE_EXIT_CODE": claude_exit})

    result = subprocess.run(
        [str(runner), *runner_args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )

    assert result.returncode == expected_exit, result.stdout
    assert "push origin master" in git_log.read_text().splitlines()
    assert expected_log in result.stdout
    assert f"finished (exit: {expected_exit})" in result.stdout


def test_cc_runner_lock_excludes_overlapping_processes(tmp_path: Path) -> None:
    runner, _, env = _prepare_cc_runner(tmp_path)
    started = tmp_path / "claude-started"
    release = tmp_path / "claude-release"
    env.update(
        {
            "CLAUDE_MODE": "hold",
            "CLAUDE_STARTED_FILE": str(started),
            "CLAUDE_RELEASE_FILE": str(release),
        }
    )

    first = subprocess.Popen(
        [str(runner)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists(), "first runner did not reach Claude"

        second = subprocess.run(
            [str(runner)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )

        assert second.returncode == 1
        assert "Another autonomous run is active" in second.stdout
    finally:
        release.touch()
        first_stdout, _ = first.communicate(timeout=5)

    assert first.returncode == 0, first_stdout

    third = subprocess.run(
        [str(runner)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
    )
    assert third.returncode == 0, third.stdout
