"""Unit tests for gptme_image_gen plugin."""

from pathlib import Path
from unittest.mock import patch

import pytest

from gptme_image_gen.tools.image_gen import (
    ImageResult,
    generate_image,
)


class TestGenerateImage:
    """Tests for generate_image function."""

    def test_invalid_provider(self):
        """Test error handling for invalid provider."""
        with pytest.raises(ValueError, match="Unknown provider"):
            generate_image(prompt="test", provider="invalid")

    def test_output_path_default(self, tmp_path, monkeypatch):
        """Test that default output path is auto-generated."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=tmp_path / "generated_20250101_120000.png",
                metadata={"model": "imagen-3-fast"},
            )

            result = generate_image(prompt="test", provider="gemini")

            # Verify auto-generated path was used
            assert result.image_path.name.startswith("generated_")
            assert result.image_path.suffix == ".png"

    def test_output_path_relative(self, tmp_path, monkeypatch):
        """Test that relative paths are converted to absolute."""
        monkeypatch.chdir(tmp_path)

        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            expected_path = tmp_path / "test.png"
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=expected_path,
                metadata={},
            )

            result = generate_image(
                prompt="test", provider="gemini", output_path="test.png"
            )

            # Path should be absolute
            assert result.image_path.is_absolute()

    def test_output_path_absolute(self, tmp_path):
        """Test that absolute paths are preserved."""
        output_path = tmp_path / "test.png"

        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=output_path,
                metadata={},
            )

            result = generate_image(
                prompt="test", provider="gemini", output_path=str(output_path)
            )

            assert result.image_path == output_path


class TestImageResult:
    """Tests for ImageResult dataclass."""

    def test_image_result_creation(self):
        """Test creating ImageResult with all fields."""
        result = ImageResult(
            provider="gemini",
            prompt="test prompt",
            image_path=Path("/tmp/test.png"),
            metadata={"model": "imagen-3-fast", "size": "1024x1024"},
        )

        assert result.provider == "gemini"
        assert result.prompt == "test prompt"
        assert result.image_path == Path("/tmp/test.png")
        assert result.metadata["model"] == "imagen-3-fast"
        assert result.metadata["size"] == "1024x1024"


@pytest.mark.slow
@pytest.mark.requires_api_keys
class TestGenerateImageIntegration:
    """Integration tests requiring real API keys."""

    def test_gemini_generation(self, skip_if_no_api_keys, temp_output_dir):
        """Test actual Gemini image generation.

        Requires GOOGLE_API_KEY environment variable.
        """
        output_path = temp_output_dir / "test_gemini.png"

        result = generate_image(
            prompt="Simple red circle on white background",
            provider="gemini",
            output_path=str(output_path),
        )

        assert result.provider == "gemini"
        assert result.image_path.exists()
        assert result.image_path.stat().st_size > 1000  # Not empty
        assert result.metadata["model"] == "imagen-3-fast"

    def test_dalle_generation(self, skip_if_no_api_keys, temp_output_dir):
        """Test actual DALL-E 3 generation.

        Requires OPENAI_API_KEY environment variable.
        """
        output_path = temp_output_dir / "test_dalle.png"

        result = generate_image(
            prompt="Simple blue square on white background",
            provider="dalle",
            output_path=str(output_path),
        )

        assert result.provider == "dalle"
        assert result.image_path.exists()
        assert result.image_path.stat().st_size > 1000  # Not empty
        assert result.metadata["model"] == "dall-e-3"

    def test_dalle2_generation(self, skip_if_no_api_keys, temp_output_dir):
        """Test actual DALL-E 2 generation.

        Requires OPENAI_API_KEY environment variable.
        """
        output_path = temp_output_dir / "test_dalle2.png"

        result = generate_image(
            prompt="Simple green triangle on white background",
            provider="dalle2",
            output_path=str(output_path),
        )

        assert result.provider == "dalle2"
        assert result.image_path.exists()
        assert result.image_path.stat().st_size > 1000
        assert result.metadata["model"] == "dall-e-2"
