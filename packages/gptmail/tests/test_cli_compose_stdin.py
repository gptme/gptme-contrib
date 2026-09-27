"""Tests for ``gptmail compose -`` reading the body from stdin.

Regression guard for the daily-email failure (2026-09-27): a large HTML digest
passed as an argv argument overflowed the shell's ARG_MAX and failed with
"Argument list too long". The fix pipes the body via stdin using the "-"
sentinel; these tests pin that the sentinel routes to stdin and that a normal
argument body is unaffected.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from gptmail import cli as cli_module
from gptmail.cli import cli


@pytest.fixture
def capture_compose(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Capture the content AgentEmail.compose receives; no real workspace/email."""
    captured: list[str] = []

    class FakeEmail:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def compose(self, to, subject, content, from_address=None):  # noqa: ANN001
            captured.append(content)
            return "<fake-message-id@local>"

    monkeypatch.setattr(cli_module, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "AgentEmail", FakeEmail)
    return captured


def test_compose_dash_reads_body_from_stdin(capture_compose: list[str]) -> None:
    body = "<html><body>" + "x" * 5000 + "</body></html>"
    result = CliRunner().invoke(cli, ["compose", "erik@example.com", "Subject", "-"], input=body)
    assert result.exit_code == 0, result.output
    assert capture_compose == [body]


def test_compose_argv_body_still_works(capture_compose: list[str]) -> None:
    result = CliRunner().invoke(cli, ["compose", "erik@example.com", "Subject", "inline body"])
    assert result.exit_code == 0, result.output
    assert capture_compose == ["inline body"]
