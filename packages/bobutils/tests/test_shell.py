"""Tests for bobutils.shell."""

from __future__ import annotations

from bobutils.shell import run_cmd


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
