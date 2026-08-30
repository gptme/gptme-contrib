"""Git-Session-Id prepare-commit-msg hook: add, preserve, no-session.

This hook already works in live agent core.hooksPath trees. The contrib
dotfiles copy is the source of truth; a future hooks deploy from this tree
must keep the same three no-surprise paths.

Tests invoke the hook against temporary commit-message files. They do not
create commits or mutate repository history.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

HOOK = (
    Path(__file__).resolve().parents[1]
    / "dotfiles"
    / ".config"
    / "git"
    / "hooks"
    / "prepare-commit-msg"
)


def _clean_env(**extra: str) -> dict[str, str]:
    """Strip inherited GIT_* vars so the hook cannot touch the host repo.

    Autonomous sessions often export GIT_DIR / GIT_COMMITTER_SESSION_ID.
    The hook reads GIT_COMMITTER_SESSION_ID; tests that need a session id
    pass it back explicitly.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra)
    return env


def _run_hook(msg_file: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOK), str(msg_file)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def test_hook_is_executable() -> None:
    assert HOOK.is_file()
    mode = HOOK.stat().st_mode
    assert mode & stat.S_IXUSR


def test_adds_trailer_when_session_id_present(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: add widget\n")

    result = _run_hook(msg, _clean_env(GIT_COMMITTER_SESSION_ID="f05eabcd"))

    assert result.returncode == 0, result.stderr
    body = msg.read_text()
    assert "feat: add widget" in body
    assert "Git-Session-Id: f05eabcd" in body


def test_preserves_existing_trailer(tmp_path: Path) -> None:
    original = "feat: add widget\n\nGit-Session-Id: already-there\n"
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(original)

    result = _run_hook(msg, _clean_env(GIT_COMMITTER_SESSION_ID="should-not-win"))

    assert result.returncode == 0, result.stderr
    assert msg.read_text() == original


def test_no_session_env_leaves_message_unchanged(tmp_path: Path) -> None:
    original = "feat: add widget\n"
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(original)

    result = _run_hook(msg, _clean_env())

    assert result.returncode == 0, result.stderr
    assert msg.read_text() == original
