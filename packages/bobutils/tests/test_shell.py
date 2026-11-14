"""Tests for bobutils.shell."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from bobutils import shell
from bobutils.shell import gh_json, gh_paginated_list, run_cmd


def test_run_cmd_basic() -> None:
    assert run_cmd(["echo", "hello"]) == "hello"


def test_run_cmd_strips_trailing_newline() -> None:
    assert run_cmd(["printf", "world\n"]) == "world"


def test_run_cmd_timeout_returns_empty() -> None:
    assert run_cmd(["sleep", "10"], timeout=0) == ""


def test_run_cmd_missing_command_returns_empty() -> None:
    assert run_cmd(["nonexistent-command-xyz-abc"]) == ""


def test_run_cmd_cwd(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = run_cmd(["pwd"], cwd=tmp_path)
    assert result == str(tmp_path)


def test_run_cmd_nonzero_exit_returns_empty() -> None:
    assert run_cmd(["sh", "-c", "printf stale; exit 1"]) == ""


def _fake_run(stdout: str = "", returncode: int = 0, exc: Exception | None = None):
    calls: list[list[str]] = []

    def run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    return run, calls


def test_gh_json_parses_output(monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = _fake_run('{"state": "OPEN"}')
    monkeypatch.setattr(shell.subprocess, "run", run)
    assert gh_json(["pr", "view", "1"]) == {"state": "OPEN"}
    assert calls == [["gh", "pr", "view", "1"]]


@pytest.mark.parametrize(
    ("stdout", "returncode", "exc"),
    [
        ('{"a": 1}', 1, None),
        ("  \n", 0, None),
        ("not json", 0, None),
        ("", 0, subprocess.TimeoutExpired(["gh"], 1)),
        ("", 0, FileNotFoundError("gh")),
    ],
)
def test_gh_json_failures_return_none(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
    exc: Exception | None,
) -> None:
    run, _ = _fake_run(stdout, returncode, exc)
    monkeypatch.setattr(shell.subprocess, "run", run)
    assert gh_json(["api", "x"]) is None


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('[[{"n": 1}, {"n": 2}], [{"n": 3}]]', [{"n": 1}, {"n": 2}, {"n": 3}]),
        ('[{"n": 1}, "junk", {"n": 2}]', [{"n": 1}, {"n": 2}]),
        ('{"not": "a list"}', []),
        ("", []),
    ],
)
def test_gh_paginated_list(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: list[dict]
) -> None:
    run, calls = _fake_run(stdout)
    monkeypatch.setattr(shell.subprocess, "run", run)
    assert gh_paginated_list(["api", "repos/o/r/pulls"]) == expected
    assert calls[0][-2:] == ["--paginate", "--slurp"]
