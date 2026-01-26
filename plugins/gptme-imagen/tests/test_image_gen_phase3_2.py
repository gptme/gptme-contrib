"""Unit tests for Phase 3.2 enhancements to gptme_imagen plugin."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gptme_imagen.tools.image_gen import (
    ImageResult,
    batch_generate,
    compare_providers,
    generate_variation,
)


class TestBatchGenerate:
    """Tests for batch_generate function."""

    def test_batch_generate_multiple_prompts(self, tmp_path):
        """Test batch generation with multiple prompts."""
        prompts = ["sunset", "mountain", "city"]

        with patch("gptme_imagen.tools.image_gen.generate_image") as mock_gen:
            mock_gen.side_effect = [
                ImageResult(
                    provider="gemini",
                    prompt=prompt,
                    image_path=tmp_path / f"{prompt}.png",
                    metadata={"model": "imagen-3-fast"},
                )
                for prompt in prompts
            ]

            results = batch_generate(prompts=prompts, provider="gemini")

            assert len(results) == 3
            assert all(isinstance(r, ImageResult) for r in results)
            assert mock_gen.call_count == 3

    def test_batch_generate_with_output_dir(self, tmp_path):
        """Test batch generation creates output directory."""
        prompts = ["test1", "test2"]
        output_dir = str(tmp_path / "batch_output")

        with patch("gptme_imagen.tools.image_gen.generate_image") as mock_gen:
            mock_gen.side_effect = [
                ImageResult(
                    provider="gemini",
                    prompt=prompt,
                    image_path=tmp_path / f"{prompt}.png",
                    metadata={},
                )
                for prompt in prompts
            ]

            results = batch_generate(
                prompts=prompts, provider="gemini", output_dir=output_dir
            )

            # Verify output directory would be created
            assert Path(output_dir).exists()
            assert len(results) == 2

    def test_batch_generate_view_integration(self, tmp_path):
        """Test batch generation with view=True displays all images."""
        prompts = ["test"]

        with patch("gptme_imagen.tools.image_gen.generate_image") as mock_gen:
            with patch("gptme.tools.vision.view_image") as mock_view:
                mock_gen.return_value = ImageResult(
                    provider="gemini",
                    prompt="test",
                    image_path=tmp_path / "test.png",
                    metadata={},
                )

                results = batch_generate(prompts=prompts, view=True)

                # View should be called once per result
                assert mock_view.call_count == 1
                assert len(results) == 1


class TestCompareProviders:
    """Tests for compare_providers function."""

    def test_compare_providers_default(self, tmp_path):
        """Test provider comparison with defaults (gemini, dalle)."""
        with patch("gptme_imagen.tools.image_gen.generate_image") as mock_gen:
            mock_gen.side_effect = [
                ImageResult(
                    provider="gemini",
                    prompt="test",
                    image_path=tmp_path / "test_gemini.png",
                    metadata={},
                ),
                ImageResult(
                    provider="dalle",
                    prompt="test",
                    image_path=tmp_path / "test_dalle.png",
                    metadata={},
                ),
            ]

            results = compare_providers(prompt="test", view=False)

            assert len(results) == 2
            assert "gemini" in results
            assert "dalle" in results
            assert mock_gen.call_count == 2

    def test_compare_providers_custom_list(self, tmp_path):
        """Test provider comparison with custom provider list."""
        with patch("gptme_imagen.tools.image_gen.generate_image") as mock_gen:
            mock_gen.side_effect = [
                ImageResult(
                    provider="dalle2",
                    prompt="test",
                    image_path=tmp_path / "test.png",
                    metadata={},
                )
            ]

            results = compare_providers(prompt="test", providers=["dalle2"], view=False)

            assert len(results) == 1
            assert "dalle2" in results

    def test_compare_providers_handles_failures(self, tmp_path):
        """Test that comparison continues if one provider fails."""

        def mock_generate(*args, **kwargs):
            provider = kwargs.get("provider")
            if provider == "gemini":
                raise Exception("API error")
            return ImageResult(
                provider=provider,
                prompt="test",
                image_path=tmp_path / f"test_{provider}.png",
                metadata={},
            )

        with patch(
            "gptme_imagen.tools.image_gen.generate_image", side_effect=mock_generate
        ):
            results = compare_providers(
                prompt="test", providers=["gemini", "dalle"], view=False
            )

            # Should still get dalle result even though gemini failed
            assert len(results) == 1
            assert "dalle" in results
            assert "gemini" not in results


class TestGenerateVariation:
    """Tests for generate_variation function."""

    def test_generate_variation_unsupported_provider(self, tmp_path):
        """Test that unsupported providers raise ValueError."""
        test_image = tmp_path / "test.png"
        test_image.write_bytes(b"fake image data")

        with pytest.raises(ValueError, match="only supported for dalle2"):
            generate_variation(image_path=str(test_image), provider="gemini")

    def test_generate_variation_missing_image(self):
        """Test that missing image file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Image not found"):
            generate_variation(image_path="nonexistent.png", provider="dalle2")

    def test_generate_variation_single(self, tmp_path, mock_api_keys):
        """Test single variation generation returns ImageResult."""
        test_image = tmp_path / "test.png"
        test_image.write_bytes(b"fake image data")

        mock_response = MagicMock()
        mock_response.data = [MagicMock(url="https://fake.url/image.png")]

        with patch("openai.OpenAI") as mock_client:
            with patch("requests.get") as mock_get:
                mock_get.return_value.content = b"generated image data"
                mock_client.return_value.images.create_variation.return_value = (
                    mock_response
                )

                result = generate_variation(
                    image_path=str(test_image), provider="dalle2", count=1
                )

                # Should return single ImageResult
                assert isinstance(result, ImageResult)
                assert not isinstance(result, list)
                assert result.provider == "dalle2"

    def test_generate_variation_multiple(self, tmp_path, mock_api_keys):
        """Test multiple variations return list of ImageResults."""
        test_image = tmp_path / "test.png"
        test_image.write_bytes(b"fake image data")

        mock_response = MagicMock()
        mock_response.data = [MagicMock(url="https://fake.url/image.png")]

        with patch("openai.OpenAI") as mock_client:
            with patch("requests.get") as mock_get:
                mock_get.return_value.content = b"generated image data"
                mock_client.return_value.images.create_variation.return_value = (
                    mock_response
                )

                results = generate_variation(
                    image_path=str(test_image), provider="dalle2", count=3
                )

                # Should return list of ImageResults
                assert isinstance(results, list)
                assert len(results) == 3
                assert all(isinstance(r, ImageResult) for r in results)
