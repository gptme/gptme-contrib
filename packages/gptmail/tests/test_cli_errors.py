"""Library ValueErrors surface as CLI errors, not tracebacks (gptme-contrib#2383)."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from gptmail import cli as cli_module
from gptmail.cli import cli


@pytest.fixture
def failing_email(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeEmail:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def send(self, message_id, force=False):  # noqa: ANN001
            raise ValueError(f"Already sent: {message_id}")

        def read_message(self, message_id):  # noqa: ANN001
            raise ValueError(f"Message not found: {message_id}")

    monkeypatch.setattr(cli_module, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "AgentEmail", FakeEmail)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["send", "<sent@local>"], "Already sent: <sent@local>"),
        (["reply", "<nope@local>", "hi"], "Message not found: <nope@local>"),
    ],
)
def test_value_error_is_a_clean_cli_error(
    failing_email: None, args: list[str], message: str
) -> None:
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    assert message in result.output
    assert isinstance(result.exception, SystemExit)
