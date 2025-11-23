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
def mock_gemini():
    """Mock Google Gemini API."""
    with patch("google.generativeai.GenerativeModel") as mock_model_class, patch(
        "google.generativeai.configure"
    ), patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}):
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_model_class.return_value = mock_model

        yield mock_model


@pytest.fixture
def mock_openai():
    """Mock OpenAI API."""
    with patch("openai.OpenAI") as mock_client_class, patch.dict(
        os.environ, {"OPENAI_API_KEY": "test_key"}
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

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_various_sizes(self, mock_genai, tmp_path):
        """Test different size specifications."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

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

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_default_path_generation(self, mock_genai):
        """Test automatic path generation when not specified."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        result = generate_image(prompt="Test", provider="gemini")

        # Should generate path like "generated_20231201_123456.png"
        assert result.image_path.name.startswith("generated_")
        assert result.image_path.suffix == ".png"
        assert result.image_path.exists()

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_nested_directory_creation(self, mock_genai, tmp_path):
        """Test creation of nested directories for output."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        nested_path = tmp_path / "deep" / "nested" / "path" / "image.png"
        result = generate_image(
            prompt="Test", provider="gemini", output_path=str(nested_path)
        )

        assert result.image_path.exists()
        assert result.image_path.parent.exists()

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_path_with_tilde_expansion(self, mock_genai):
        """Test ~ expansion in paths."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

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

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_missing_api_key_gemini(self, mock_genai):
        """Test error when Gemini API key missing."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="GOOGLE_API_KEY"):
                generate_image(prompt="Test", provider="gemini")

    @patch("gptme_image_gen.tools.image_gen.OpenAI")
    def test_missing_api_key_dalle(self, mock_openai):
        """Test error when OpenAI API key missing."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="OPENAI_API_KEY"):
                generate_image(prompt="Test", provider="dalle")

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_api_failure_handling(self, mock_genai):
        """Test graceful handling of API failures."""
        mock_model = MagicMock()
        mock_model.generate_content.side_effect = Exception("API Error")
        mock_genai.GenerativeModel.return_value = mock_model

        with pytest.raises(Exception, match="API Error"):
            generate_image(prompt="Test", provider="gemini")

    @patch("gptme_image_gen.tools.image_gen.OpenAI")
    def test_missing_image_data(self, mock_openai, tmp_path):
        """Test error when API returns no image data."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_image = MagicMock()
        # No url or b64_json attributes
        mock_image.url = None
        mock_image.b64_json = None
        mock_response.data = [mock_image]
        mock_client.images.generate.return_value = mock_response
        mock_openai.return_value = mock_client

        with pytest.raises(ValueError, match="No image data"):
            generate_image(
                prompt="Test", provider="dalle", output_path=str(tmp_path / "test.png")
            )


class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_very_long_prompt(self, mock_genai, tmp_path):
        """Test with extremely long prompt."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        long_prompt = "Test prompt " * 1000  # Very long
        output_path = tmp_path / "test.png"

        result = generate_image(
            prompt=long_prompt, provider="gemini", output_path=str(output_path)
        )

        assert result.prompt == long_prompt
        assert output_path.exists()

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_unicode_in_prompt(self, mock_genai, tmp_path):
        """Test Unicode characters in prompt."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        unicode_prompt = "Test 测试 тест テスト 🎨🖼️"
        output_path = tmp_path / "test.png"

        result = generate_image(
            prompt=unicode_prompt, provider="gemini", output_path=str(output_path)
        )

        assert result.prompt == unicode_prompt

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_special_characters_in_path(self, mock_genai, tmp_path):
        """Test special characters in output path."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        # Test path with spaces and special chars
        output_path = tmp_path / "test image (v2).png"

        result = generate_image(
            prompt="Test", provider="gemini", output_path=str(output_path)
        )

        assert result.image_path.exists()


@pytest.mark.slow
class TestPerformance:
    """Performance tests for image generation."""

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_generation_time(self, mock_genai, tmp_path):
        """Test that generation completes in reasonable time."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        output_path = tmp_path / "test.png"

        start_time = time.time()
        generate_image(prompt="Test", provider="gemini", output_path=str(output_path))
        elapsed = time.time() - start_time

        # Should complete quickly with mocked API
        assert elapsed < 5.0, f"Generation took {elapsed}s, expected < 5s"

    @patch("gptme_image_gen.tools.image_gen.genai")
    def test_multiple_generations(self, mock_genai, tmp_path):
        """Test multiple generations in sequence."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.images = [b"fake_image_data"]
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

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
