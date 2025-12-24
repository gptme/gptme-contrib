"""Feature tests for image_gen plugin.

Tests specific features, error handling, edge cases, and performance.
Phase 4: End-to-end feature validation.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from gptme_image_gen.tools.image_gen import ImageResult, generate_image


# Fixture to provide common mocks
@pytest.fixture
def mock_gemini(tmp_path):
    """Mock Google Gemini API by mocking the internal _generate_gemini function."""
    with patch(
        "gptme_image_gen.tools.image_gen._generate_gemini"
    ) as mock_generate_gemini:
        # Create a fake image result
        def fake_generate(prompt, size, quality, output_path, images=None):
            from pathlib import Path

            from gptme_image_gen.tools.image_gen import ImageResult

            # Create actual file
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake_image_data")

            return ImageResult(
                provider="gemini",
                prompt=prompt,
                image_path=path,
                metadata={
                    "model": "gemini-3-pro-image-preview",
                    "size": size,
                    "images": [str(p) for p in images] if images else None,
                },
            )

        mock_generate_gemini.side_effect = fake_generate
        yield mock_generate_gemini


@pytest.fixture
def mock_openai():
    """Mock OpenAI API."""
    with (
        patch("openai.OpenAI") as mock_client_class,
        patch.dict(os.environ, {"OPENAI_API_KEY": "test_key"}),
    ):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_image = MagicMock()
        mock_image.url = "https://example.com/image.png"
        mock_image.b64_json = "ZmFrZV9pbWFnZV9kYXRh"
        mock_response.data = [mock_image]
        mock_client.images.generate.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Mock requests.get for URL download
        with patch("requests.get") as mock_get:
            mock_get.return_value.content = b"fake_image_data"
            yield mock_client


class TestProviderFeatures:
    """Test all provider-specific features."""

    def test_gemini_provider(self, mock_gemini, tmp_path):
        """Test Gemini provider with all options."""

        output_path = tmp_path / "test.png"
        result = generate_image(
            prompt="Test prompt",
            provider="gemini",
            size="1024x1024",
            quality="standard",
            output_path=str(output_path),
        )

        assert isinstance(result, ImageResult)
        assert result.provider == "gemini"
        assert result.prompt == "Test prompt"
        assert result.image_path == output_path
        assert output_path.exists()

    def test_dalle_provider(self, mock_openai, tmp_path):
        """Test DALL-E 3 provider."""

        output_path = tmp_path / "test.png"
        result = generate_image(
            prompt="Test prompt",
            provider="dalle2",
            output_path=str(output_path),
        )

        assert result.provider == "dalle2"
        assert result.metadata["model"] == "dall-e-2"


class TestSizeOptions:
    """Test different image size configurations."""

    def test_various_sizes(self, mock_gemini, tmp_path):
        """Test different size specifications."""
        sizes = ["1024x1024", "512x512", "256x256", "1792x1024"]

        for size in sizes:
            output_path = tmp_path / f"test_{size}.png"
            result = generate_image(
                prompt="Test",
                provider="gemini",
                size=size,
                output_path=str(output_path),
            )

            assert result.metadata["size"] == size
            assert output_path.exists()


class TestOutputPathHandling:
    """Test output path features and edge cases."""

    def test_default_path_generation(self, mock_gemini):
        """Test automatic path generation when not specified."""
        result = generate_image(prompt="Test", provider="gemini")

        # Should generate path like "generated_20231201_123456.png"
        assert result.image_path.name.startswith("generated_")
        assert result.image_path.suffix == ".png"
        assert result.image_path.exists()

    def test_nested_directory_creation(self, mock_gemini, tmp_path):
        """Test creation of nested directories for output."""
        nested_path = tmp_path / "deep" / "nested" / "path" / "image.png"
        result = generate_image(
            prompt="Test", provider="gemini", output_path=str(nested_path)
        )

        assert result.image_path.exists()
        assert result.image_path.parent.exists()

    def test_path_with_tilde_expansion(self, mock_gemini):
        """Test ~ expansion in paths."""
        result = generate_image(
            prompt="Test", provider="gemini", output_path="~/test_image.png"
        )

        assert result.image_path.is_absolute()
        assert "~" not in str(result.image_path)


class TestErrorHandling:
    """Test error handling for invalid inputs and failures."""

    def test_invalid_provider(self):
        """Test error on invalid provider name."""
        with pytest.raises(ValueError, match="Unknown provider"):
            generate_image(prompt="Test", provider="invalid_provider")

    @patch("gptme_image_gen.tools.image_gen._generate_gemini")
    def test_missing_api_key_gemini(self, mock_generate):
        """Test error when Gemini API key missing."""
        # Mock will raise the API key error as the real function would
        mock_generate.side_effect = ValueError(
            "GOOGLE_API_KEY environment variable not set"
        )
        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            generate_image(prompt="Test", provider="gemini")

    @patch("gptme_image_gen.tools.image_gen._generate_dalle")
    def test_missing_api_key_dalle(self, mock_generate):
        """Test error when OpenAI API key missing."""
        mock_generate.side_effect = ValueError(
            "OPENAI_API_KEY environment variable not set"
        )
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            generate_image(prompt="Test", provider="dalle")

    @patch("gptme_image_gen.tools.image_gen._generate_gemini")
    def test_api_failure_handling(self, mock_generate):
        """Test graceful handling of API failures."""
        mock_generate.side_effect = Exception("API Error")
        with pytest.raises(RuntimeError, match="API Error"):
            generate_image(prompt="Test", provider="gemini")

    @patch("gptme_image_gen.tools.image_gen._generate_dalle")
    def test_missing_image_data(self, mock_generate, tmp_path):
        """Test error when API returns no image data."""
        mock_generate.side_effect = ValueError("No image data received from API")
        with pytest.raises(RuntimeError, match="No image data"):
            generate_image(
                prompt="Test", provider="dalle", output_path=str(tmp_path / "test.png")
            )


class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    def test_very_long_prompt(self, mock_gemini, tmp_path):
        """Test with extremely long prompt."""
        long_prompt = "Test prompt " * 1000  # Very long
        output_path = tmp_path / "test.png"

        result = generate_image(
            prompt=long_prompt, provider="gemini", output_path=str(output_path)
        )

        assert result.prompt == long_prompt
        assert output_path.exists()

    def test_unicode_in_prompt(self, mock_gemini, tmp_path):
        """Test Unicode characters in prompt."""
        unicode_prompt = "Test 测试 测试 テスト 🎨🖼️"
        output_path = tmp_path / "test.png"

        result = generate_image(
            prompt=unicode_prompt, provider="gemini", output_path=str(output_path)
        )

        assert result.prompt == unicode_prompt

    def test_special_characters_in_path(self, mock_gemini, tmp_path):
        """Test special characters in output path."""
        # Test path with spaces and special chars
        output_path = tmp_path / "test image (v2).png"

        result = generate_image(
            prompt="Test", provider="gemini", output_path=str(output_path)
        )

        assert result.image_path.exists()


@pytest.mark.slow
class TestPerformance:
    """Performance tests for image generation."""

    def test_generation_time(self, mock_gemini, tmp_path):
        """Test that generation completes in reasonable time."""
        output_path = tmp_path / "test.png"

        start_time = time.time()
        generate_image(prompt="Test", provider="gemini", output_path=str(output_path))
        elapsed = time.time() - start_time

        # Should complete quickly with mocked API
        assert elapsed < 5.0, f"Generation took {elapsed}s, expected < 5s"

    def test_multiple_generations(self, mock_gemini, tmp_path):
        """Test multiple generations in sequence."""
        start_time = time.time()

        # Generate 5 images
        for i in range(5):
            output_path = tmp_path / f"test_{i}.png"
            result = generate_image(
                prompt=f"Test {i}", provider="gemini", output_path=str(output_path)
            )
            assert result.image_path.exists()

        elapsed = time.time() - start_time

        # All 5 should complete reasonably quickly
        assert elapsed < 10.0, f"5 generations took {elapsed}s, expected < 10s"
