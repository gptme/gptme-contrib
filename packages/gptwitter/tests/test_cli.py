from click.testing import CliRunner

from gptwitter.cli import cli  # type: ignore[import-not-found]


def test_cli_help_smoke() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "Twitter Tool" in result.output
