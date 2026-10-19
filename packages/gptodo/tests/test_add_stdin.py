"""``gptodo add`` must not block on a non-tty stdin that carries no body."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import _read_piped_body, cli


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tasks").mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)
    return tmp_path


def test_open_empty_pipe_returns_promptly(workspace: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "gptodo", "add", "Task filed from a harness pipe"],
        cwd=workspace,
        stdin=subprocess.PIPE,  # never written, never closed: the harness shape
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = _wait(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    created = workspace / "tasks" / "task-filed-from-a-harness-pipe.md"
    assert created.exists()
    assert "## " not in created.read_text()
    assert out.strip().splitlines()[-1] == "task-filed-from-a-harness-pipe"


def _wait(proc: subprocess.Popen[str]) -> tuple[str, str]:
    # Do not close stdin: the point is that the child must finish while the
    # pipe stays open with no writer activity.
    proc.wait(timeout=20)
    assert proc.stdout is not None and proc.stderr is not None
    return proc.stdout.read(), proc.stderr.read()


def test_piped_body_is_still_read(workspace: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "gptodo", "add", "Task with body"],
        cwd=workspace,
        input="## Steps\n- [ ] one\n",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "- [ ] one" in (workspace / "tasks" / "task-with-body.md").read_text()


def test_read_piped_body_without_fileno_falls_back_to_read() -> None:
    assert _read_piped_body(io.StringIO("hello\n")) == "hello"


def test_read_piped_body_dev_null_is_empty() -> None:
    with open(os.devnull) as devnull:
        assert _read_piped_body(devnull) == ""


def test_read_piped_body_falls_back_when_select_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows ``select()`` only accepts sockets; a piped body must still be read
    # instead of being silently dropped.
    import select

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("select() only accepts sockets on Windows")

    monkeypatch.setattr(select, "select", _raise)
    body_path = tmp_path / "body.txt"
    body_path.write_text("body\n")
    with open(body_path) as stream:
        assert _read_piped_body(stream) == "body"


def test_read_piped_body_select_unsupported_reads_a_full_pipe_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import select

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("select() only accepts sockets on Windows")

    monkeypatch.setattr(select, "select", _raise)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"line one\nline two\n")
        os.close(write_fd)
        with os.fdopen(read_fd) as stream:
            assert _read_piped_body(stream, timeout=0.2) == "line one\nline two"
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_read_piped_body_select_unsupported_does_not_block_on_open_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Windows fallback must not reintroduce the hang for a pipe that never
    # reaches EOF.
    import select

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("select() only accepts sockets on Windows")

    monkeypatch.setattr(select, "select", _raise)
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as stream:
            assert _read_piped_body(stream, timeout=0.2) == ""
    finally:
        os.close(write_fd)


def test_cli_runner_input_still_becomes_body(workspace: Path) -> None:
    result = CliRunner().invoke(cli, ["add", "Runner task"], input="body line\n")
    assert result.exit_code == 0, result.output
    assert "body line" in (workspace / "tasks" / "runner-task.md").read_text()
