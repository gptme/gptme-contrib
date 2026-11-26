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
        b"fix: doc ------------------------ >8 ------------------------\n</think>\n",
        b"fix: thing\n\n# ------------------------ >8 ------------------------\n"
        b" </think>\n",
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
        b"fix: docs\n\n- Example:\n\n    ```xml\n    <think>\n    ```\n",
        b"fix: docs\n\n1. Step\n\t```\n\t</think>\n\t```\n",
        b"fix: thing\n\n# ------------------------ >8 ------------------------\n"
        b"diff --git a/example b/example\n--- a/example\n+++ b/example\n"
        b"@@ -1 +1 @@\n-</think>\n+<think>\n",
        # `git commit -v`: an unchanged context line strips to a bare tag
        # (#2346 item 11); the verbose suffix is always cut by git.
        b"fix: thing\n\n# ------------------------ >8 ------------------------\n"
        b"# Do not modify or remove the line above.\n"
        b"# Everything below it will be ignored.\n"
        b"diff --git a/doc.txt b/doc.txt\n--- a/doc.txt\n+++ b/doc.txt\n"
        b"@@ -1,2 +1,2 @@\n <think>\n-old\n+new\n",
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

    scissors = git(
        "commit",
        "--allow-empty",
        "-m",
        "fix: x\n\n# ------------------------ >8 ------------------------\n</think>",
    )
    assert scissors.returncode != 0
    assert "rejected" in scissors.stderr
    assert git("rev-parse", "--verify", "HEAD").returncode != 0

    synthetic_diff = git(
        "commit",
        "--allow-empty",
        "-m",
        "fix: x\n\n# ------------------------ >8 ------------------------\n"
        "diff --git a/f b/f\n</think>",
    )
    assert synthetic_diff.returncode != 0
    assert "rejected" in synthetic_diff.stderr
    assert git("rev-parse", "--verify", "HEAD").returncode != 0

    ok = git("commit", "--allow-empty", "-m", "fix: handle `</think>` leaks")
    assert ok.returncode == 0, ok.stderr


@pytest.mark.parametrize("editor", ["rewrite", ":"])
@pytest.mark.parametrize("comment_char", ["#", "d"])
def test_verbose_commit_with_tag_in_diff_context_passes(
    tmp_path: Path, comment_char: str, editor: str
) -> None:
    """#2346 item 11: `git commit -v` must not reject on unchanged diff context."""
    repo = tmp_path / "repo"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        # Prepend a subject to the editor template (which holds the -v diff).
        GIT_EDITOR='sh -c \'printf "fix: y\\n" | cat - "$1" > "$1.t"'
        ' && mv "$1.t" "$1"\' sh',
    )
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "commit-msg").symlink_to(HOOK)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                f"core.hooksPath={hooks}",
                "-c",
                f"core.commentChar={comment_char}",
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    (repo / "doc.txt").write_text("<think>\nold\n")
    git("add", "doc.txt")
    assert git("commit", "-q", "-m", "docs: x").returncode == 0
    (repo / "doc.txt").write_text("<think>\nnew\n")

    if editor == ":":
        env["GIT_EDITOR"] = ":"
        result = git("commit", "-q", "-a", "-v", "-e", "-m", "fix: y")
    else:
        result = git("commit", "-q", "-a", "-v")

    assert result.returncode == 0, result.stderr
    assert git("log", "-1", "--format=%s").stdout.strip() == "fix: y"
