"""Git-Session-Id prepare-commit-msg hook: add, preserve, recover.

This hook already works in live agent core.hooksPath trees. The contrib
dotfiles copy is the source of truth.

Tests invoke the hook against temporary commit-message files. They do not
create commits or mutate repository history.
"""

from __future__ import annotations

import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parents[1]
    / "dotfiles"
    / ".config"
    / "git"
    / "hooks"
    / "prepare-commit-msg"
)
resolve_session_id = runpy.run_path(str(HOOK))["resolve_session_id"]

SCRUBBED = (
    "GIT_COMMITTER_SESSION_ID",
    "AGENT_SESSION_ID",
    "BOB_SESSION_ID",
    "GIT_SAFE_COMMIT_SESSION_ID",
    "CC_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)


def _clean_env(**extra: str) -> dict[str, str]:
    """Strip inherited GIT_* vars so the hook cannot touch the host repo.

    Autonomous sessions often export GIT_DIR / GIT_COMMITTER_SESSION_ID.
    Tests that need a session id pass it back explicitly. Session-identity
    keys are dropped unless the caller sets them.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for key in (*SCRUBBED, "AGENT_SESSION_TRAILER"):
        env.pop(key, None)
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


def test_agent_session_id_is_used_when_git_env_missing(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: add widget\n")

    result = _run_hook(msg, _clean_env(AGENT_SESSION_ID="agent42"))

    assert result.returncode == 0, result.stderr
    assert "Git-Session-Id: agent42" in msg.read_text()


def test_bob_session_id_remains_a_compatibility_fallback(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: add widget\n")

    result = _run_hook(msg, _clean_env(BOB_SESSION_ID="fallback42"))

    assert result.returncode == 0, result.stderr
    assert "Git-Session-Id: fallback42" in msg.read_text()


def test_preserves_existing_trailer(tmp_path: Path) -> None:
    original = "feat: add widget\n\nGit-Session-Id: already-there\n"
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(original)

    result = _run_hook(msg, _clean_env(GIT_COMMITTER_SESSION_ID="should-not-win"))

    assert result.returncode == 0, result.stderr
    assert msg.read_text() == original


def test_preserves_plural_trailers_without_claiming_pusher(tmp_path: Path) -> None:
    original = "sync: source sessions\n\nGit-Session-Ids: first, second\n"
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(original)

    result = _run_hook(msg, _clean_env(GIT_COMMITTER_SESSION_ID="pusher"))

    assert result.returncode == 0, result.stderr
    assert msg.read_text() == original


def test_opt_out_env_skips_even_when_session_id_is_set(tmp_path: Path) -> None:
    original = "feat: add widget\n"
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(original)

    result = _run_hook(
        msg,
        _clean_env(
            GIT_COMMITTER_SESSION_ID="f05eabcd",
            AGENT_SESSION_TRAILER="0",
        ),
    )

    assert result.returncode == 0, result.stderr
    assert msg.read_text() == original


def test_missing_message_file_is_a_noop() -> None:
    result = subprocess.run(
        [str(HOOK), "/nonexistent/COMMIT_EDITMSG"],
        capture_output=True,
        text=True,
        timeout=10,
        env=_clean_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    not Path("/proc/self/cgroup").is_file(),
    reason="real ancestor recovery requires Linux procfs cgroup data",
)
def test_hook_recovers_identity_from_parent_after_env_is_scrubbed(
    tmp_path: Path,
) -> None:
    message = tmp_path / "message"
    message.write_text("fix: parent identity\n")
    env = _clean_env(GIT_COMMITTER_SESSION_ID="parent42", AGENT_SESSION_ID="parent42")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, subprocess, sys; "
            "env = {k: v for k, v in os.environ.items() if k not in "
            "sys.argv[3].split(',')}; "
            "subprocess.run([sys.argv[1], sys.argv[2]], env=env, check=True)",
            str(HOOK),
            str(message),
            ",".join(SCRUBBED),
        ],
        env=env,
        check=True,
    )
    assert "Git-Session-Id: parent42" in message.read_text()


def _process(
    proc: Path,
    pid: int,
    parent: int,
    env: str = "",
    cgroup: str = "0::/user.slice/agent-autonomous.service\n",
) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "stat").write_text(f"{pid} (a command (with) spaces) S {parent} 0 0\n")
    (directory / "cgroup").write_text(cgroup)
    (directory / "environ").write_text(env)


def test_unknown_unit_does_not_become_session_identity(tmp_path: Path) -> None:
    _process(tmp_path, 20, 1)
    assert resolve_session_id({}, proc_root=tmp_path, pid=20) is None


def test_recovery_stops_at_cgroup_boundary(tmp_path: Path) -> None:
    _process(tmp_path, 20, 10)
    _process(tmp_path, 10, 1, "AGENT_SESSION_ID=unrelated\0", "0::/other.service\n")
    assert resolve_session_id({}, proc_root=tmp_path, pid=20) is None


def test_nearest_ancestor_wins_over_outer_session(tmp_path: Path) -> None:
    _process(tmp_path, 30, 20)
    _process(tmp_path, 20, 10, "AGENT_SESSION_ID=inner\0")
    _process(tmp_path, 10, 1, "AGENT_SESSION_ID=outer\0")
    assert resolve_session_id({}, proc_root=tmp_path, pid=30) == "inner"
    assert (
        resolve_session_id(
            {"AGENT_SESSION_ID": "explicit"},
            proc_root=tmp_path,
            pid=30,
        )
        == "explicit"
    )


def test_harness_state_maps_exact_session_id(tmp_path: Path) -> None:
    (tmp_path / "native-uuid.json").write_text('{"agent_session_id": "mapped42"}')
    assert (
        resolve_session_id(
            {"CC_SESSION_ID": "native-uuid"},
            state_dir=tmp_path,
            proc_root=tmp_path,
        )
        == "mapped42"
    )
    assert (
        resolve_session_id(
            {"CC_SESSION_ID": "other-uuid"},
            state_dir=tmp_path,
            proc_root=tmp_path,
        )
        is None
    )


def test_harness_state_reads_bob_session_id_compat_field(tmp_path: Path) -> None:
    (tmp_path / "native-uuid.json").write_text('{"bob_session_id": "mapped-bob"}')
    assert (
        resolve_session_id(
            {"CC_SESSION_ID": "native-uuid"},
            state_dir=tmp_path,
            proc_root=tmp_path,
        )
        == "mapped-bob"
    )


def test_invalid_identity_and_broken_proc_are_unknown(tmp_path: Path) -> None:
    assert (
        resolve_session_id(
            {"AGENT_SESSION_ID": "a\nForged: trailer"},
            proc_root=tmp_path,
        )
        is None
    )
    _process(tmp_path, 20, 1)
    (tmp_path / "20/stat").write_text("malformed")
    assert resolve_session_id({}, proc_root=tmp_path, pid=20) is None


def test_non_utf8_trailer_does_not_block_or_corrupt_message(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    original = b"feat: encoding\n\nSigned-off-by: Ren\xe9 <rene@example.com>\n"
    msg.write_bytes(original)
    result = _run_hook(msg, _clean_env(GIT_COMMITTER_SESSION_ID="encoding42"))
    assert result.returncode == 0, result.stderr
    assert b"Signed-off-by: Ren\xe9 <rene@example.com>" in msg.read_bytes()
    assert b"Git-Session-Id: encoding42" in msg.read_bytes()


def test_parent_mapping_uses_parents_tmpdir(tmp_path: Path) -> None:
    state = tmp_path / "parent-tmp/agent-provenance-state"
    state.mkdir(parents=True)
    (state / "native42.json").write_text('{"agent_session_id": "parent-mapped42"}')
    proc = tmp_path / "proc"
    _process(proc, 30, 20)
    _process(proc, 20, 1, f"CC_SESSION_ID=native42\0TMPDIR={state.parent}\0")
    assert resolve_session_id({}, proc_root=proc, pid=30) == "parent-mapped42"


def test_clean_test_env_does_not_inherit_opt_out(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SESSION_TRAILER", "0")
    inherited = "AGENT_SESSION_TRAILER" in _clean_env()
    assert not inherited
