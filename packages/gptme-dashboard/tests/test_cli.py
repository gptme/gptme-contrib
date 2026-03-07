"""Tests for CLI backward compatibility and subcommands."""

import textwrap
from pathlib import Path

from click.testing import CliRunner

from gptme_dashboard.cli import main


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace for CLI testing."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "CLITest"
        """)
    )
    (tmp_path / "lessons").mkdir()
    return tmp_path


def test_cli_generate_subcommand(tmp_path: Path):
    """Test explicit 'generate' subcommand."""
    ws = _make_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["generate", "--workspace", str(ws), "--json"])
    assert result.exit_code == 0
    assert "CLITest" in result.output


def test_cli_backward_compat_no_subcommand(tmp_path: Path):
    """Test that running without subcommand defaults to 'generate'."""
    ws = _make_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--workspace", str(ws), "--json"])
    assert result.exit_code == 0
    assert "CLITest" in result.output


def test_cli_help():
    """Test that --help works."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "generate" in result.output
    assert "serve" in result.output


def test_cli_generate_help():
    """Test that generate --help works."""
    runner = CliRunner()
    result = runner.invoke(main, ["generate", "--help"])
    assert result.exit_code == 0
    assert "--workspace" in result.output
    assert "--json" in result.output


def test_cli_serve_help():
    """Test that serve --help works."""
    runner = CliRunner()
    result = runner.invoke(main, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output
    assert "--host" in result.output
