"""Tests for the standalone CLI."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner
from gptme_imagen.cli import cli
from gptme_imagen.tools.image_gen import ImageResult


def test_cli_help():
    """CLI shows help without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Multi-provider image generation CLI" in result.output


def test_cli_generate_help():
    """Generate subcommand shows help."""
    runner = CliRunner()
    result = runner.invoke(cli, ["generate", "--help"])
    assert result.exit_code == 0
    assert "PROMPT" in result.output
    assert "--provider" in result.output
    assert "--style" in result.output
    assert "--images" in result.output


def test_cli_styles():
    """Styles command lists presets."""
    runner = CliRunner()
    result = runner.invoke(cli, ["styles"])
    assert result.exit_code == 0
    assert "photo" in result.output
    assert "watercolor" in result.output
    assert "technical-diagram" in result.output


@patch("gptme_imagen.cli.generate_image")
def test_cli_generate_basic(mock_gen):
    """Generate command calls generate_image correctly."""
    mock_result = ImageResult(
        provider="gemini",
        prompt="test prompt",
        image_path=__import__("pathlib").Path("/tmp/test.png"),
        metadata={"model": "gemini-3-pro-image-preview"},
    )
    mock_gen.return_value = mock_result

    runner = CliRunner()
    result = runner.invoke(cli, ["generate", "a sunset over mountains"])
    assert result.exit_code == 0
    assert "Saved:" in result.output
    mock_gen.assert_called_once()
    call_kwargs = mock_gen.call_args[1]
    assert call_kwargs["prompt"] == "a sunset over mountains"
    assert call_kwargs["provider"] == "gemini"


@patch("gptme_imagen.cli.generate_image")
def test_cli_generate_with_options(mock_gen):
    """Generate command passes options correctly."""
    mock_result = ImageResult(
        provider="dalle",
        prompt="logo",
        image_path=__import__("pathlib").Path("/tmp/logo.png"),
        metadata={"model": "dall-e-3"},
    )
    mock_gen.return_value = mock_result

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "generate",
            "tech logo",
            "--provider",
            "dalle",
            "--style",
            "flat-design",
            "--quality",
            "hd",
            "--count",
            "2",
            "--enhance",
            "--output",
            "/tmp/logo.png",
        ],
    )
    assert result.exit_code == 0
    call_kwargs = mock_gen.call_args[1]
    assert call_kwargs["provider"] == "dalle"
    assert call_kwargs["style"] == "flat-design"
    assert call_kwargs["quality"] == "hd"
    assert call_kwargs["count"] == 2
    assert call_kwargs["enhance"] is True
    assert call_kwargs["output_path"] == "/tmp/logo.png"


@patch("gptme_imagen.cli.generate_image")
def test_cli_generate_with_images(mock_gen):
    """Generate command passes reference images."""
    mock_result = ImageResult(
        provider="gemini",
        prompt="modify",
        image_path=__import__("pathlib").Path("/tmp/out.png"),
        metadata={},
    )
    mock_gen.return_value = mock_result

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["generate", "change background", "-i", "photo1.png", "-i", "photo2.png"],
    )
    assert result.exit_code == 0
    call_kwargs = mock_gen.call_args[1]
    assert call_kwargs["images"] == ["photo1.png", "photo2.png"]


@patch("gptme_imagen.cli.generate_image")
def test_cli_generate_error_handling(mock_gen):
    """Generate command handles errors gracefully."""
    mock_gen.side_effect = ValueError("GOOGLE_API_KEY not set")

    runner = CliRunner()
    result = runner.invoke(cli, ["generate", "test"])
    assert result.exit_code != 0
    assert "GOOGLE_API_KEY not set" in result.output


def test_cli_cost_empty():
    """Cost command handles empty records."""
    runner = CliRunner()
    result = runner.invoke(cli, ["cost"])
    assert result.exit_code == 0


def test_cli_history_empty():
    """History command handles empty records."""
    runner = CliRunner()
    result = runner.invoke(cli, ["history"])
    assert result.exit_code == 0


def test_import_without_gptme():
    """Core functions importable without gptme."""
    from gptme_imagen.tools.image_gen import (
        _HAS_GPTME,
        STYLE_PRESETS,
        generate_image,
        image_gen_tool,
    )

    assert callable(generate_image)
    assert isinstance(STYLE_PRESETS, dict)
    # image_gen_tool may be None if gptme not installed
    if not _HAS_GPTME:
        assert image_gen_tool is None
