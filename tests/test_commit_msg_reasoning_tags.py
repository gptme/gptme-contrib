"""commit-msg hook: reject bare reasoning-tag lines (gptme/gptme#4803)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parents[1]
    / "dotfiles"
    / ".config"
    / "git"
    / "hooks"
    / "commit-msg"
)


def _run(tmp_path: Path, body: bytes) -> tuple[subprocess.CompletedProcess[str], bytes]:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_bytes(body)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        [str(HOOK), str(msg)], capture_output=True, text=True, timeout=10, env=env
    )
    return result, msg.read_bytes()


@pytest.mark.parametrize(
    "body",
    [
        b"fix: thing\n</think>\n\nbody\n",
        b"fix: thing\n\nbody\n  <think>  \n",
        b"fix: caf\xe9\n\n\t</think>\r\n",
        b"fix: thing\n\n```xml\n<think>\n```\n</think>\n",
        b"fix: thing\n\n```\nunclosed ``` x\n```\n</think>\n",
    ],
)
def test_bare_tag_line_rejects_and_leaves_message_untouched(
    tmp_path: Path, body: bytes
) -> None:
    result, after = _run(tmp_path, body)

    assert result.returncode == 1
    assert "rejected" in result.stderr
    assert after == body


@pytest.mark.parametrize(
    "body",
    [
        b"fix: handle `</think>` in streams\n\nstrip <think> inline only\n",
        b"fix: docs\n\n```xml\n<think>\nexample\n</think>\n```\n",
        b"fix: docs\n\n  ~~~~\n</think>\n~~~\n<think>\n  ~~~~\n",
        b"fix: docs\n\n- Example:\n  ```xml\n  <think>\n  ```\n",
        b"fix: thing\n\n# ------------------------ >8 ------------------------\n"
        b" </think>\n",
        b"",
    ],
)
def test_inline_fenced_and_diff_mentions_pass(tmp_path: Path, body: bytes) -> None:
    result, after = _run(tmp_path, body)

    assert result.returncode == 0, result.stderr
    assert after == body


def test_reports_line_numbers(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, b"fix: x\n<think>\nok\n</think>\n")

    assert "at 2, 4" in result.stderr


def test_real_commit_is_blocked_then_allowed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "commit-msg").symlink_to(HOOK)

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), "-c", f"core.hooksPath={hooks}", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    blocked = git("commit", "--allow-empty", "-m", "fix: x\n\n</think>")
    assert blocked.returncode != 0
    assert "rejected" in blocked.stderr
    assert git("rev-parse", "--verify", "HEAD").returncode != 0

    ok = git("commit", "--allow-empty", "-m", "fix: handle `</think>` leaks")
    assert ok.returncode == 0, ok.stderr
