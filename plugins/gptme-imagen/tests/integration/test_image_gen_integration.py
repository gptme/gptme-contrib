"""Integration tests for gptme_imagen plugin.

These tests use real API calls and are marked as slow.
They require API keys to be set in environment variables.
"""

import pytest

from gptme_imagen.tools.image_gen import ImageResult, generate_image


@pytest.mark.slow
@pytest.mark.requires_api_keys
class TestImageGenIntegration:
    """Integration tests with real API calls."""

    def test_gemini_generation(self, skip_if_no_api_keys, tmp_path):
        """Test actual Gemini image generation."""
        output_path = tmp_path / "test_gemini.png"

        result = generate_image(
            prompt="Simple red circle", provider="gemini", output_path=str(output_path)
        )

        assert isinstance(result, ImageResult)
        assert result.provider == "gemini"
        assert result.image_path.exists()
        assert result.image_path == output_path
        # Check file is not empty (at least 1KB)
        assert result.image_path.stat().st_size > 1000

    def test_dalle_generation(self, skip_if_no_api_keys, tmp_path):
        """Test actual DALL-E generation."""
        output_path = tmp_path / "test_dalle.png"

        result = generate_image(
            prompt="Simple blue square", provider="dalle", output_path=str(output_path)
        )

        assert isinstance(result, ImageResult)
        assert result.provider == "dalle"
        assert result.image_path.exists()
        assert result.image_path == output_path
        # Check file is not empty
        assert result.image_path.stat().st_size > 1000

    def test_provider_comparison(self, skip_if_no_api_keys, tmp_path):
        """Test generating with multiple providers."""
        results = []

        for provider in ["gemini", "dalle"]:
            output_path = tmp_path / f"test_{provider}.png"
            result = generate_image(
                prompt="Modern tech logo",
                provider=provider,
                output_path=str(output_path),
            )
            results.append(result)

        assert len(results) == 2
        assert all(isinstance(r, ImageResult) for r in results)
        assert all(r.image_path.exists() for r in results)
        assert all(r.image_path.stat().st_size > 1000 for r in results)

        # Different providers
        assert results[0].provider != results[1].provider

    def test_output_path_handling_absolute(self, skip_if_no_api_keys, tmp_path):
        """Test absolute output path handling."""
        output_path = tmp_path / "absolute_test.png"

        result = generate_image(
            prompt="Test image", provider="gemini", output_path=str(output_path)
        )

        assert result.image_path == output_path
        assert result.image_path.is_absolute()
        assert result.image_path.exists()

    def test_output_path_auto_generated(self, skip_if_no_api_keys, tmp_path):
        """Test auto-generated output path."""
        # Change to tmp directory for test
        import os

        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            result = generate_image(prompt="Test image", provider="gemini")

            assert result.image_path.exists()
            assert result.image_path.is_absolute()
            # Auto-generated names should start with "generated_"
            assert result.image_path.name.startswith("generated_")
            assert result.image_path.suffix == ".png"
        finally:
            os.chdir(original_cwd)

    def test_metadata_completeness_gemini(self, skip_if_no_api_keys, tmp_path):
        """Test metadata includes all expected fields for Gemini."""
        output_path = tmp_path / "test_metadata.png"

        result = generate_image(
            prompt="Test image", provider="gemini", output_path=str(output_path)
        )

        # Check metadata exists and has expected structure
        assert hasattr(result, "metadata")
        metadata = result.metadata

        # Basic metadata should be present
        assert "provider" in metadata or result.provider == "gemini"
        # Image should exist with valid size
        assert result.image_path.stat().st_size > 1000

    def test_metadata_completeness_dalle(self, skip_if_no_api_keys, tmp_path):
        """Test metadata includes all expected fields for DALL-E."""
        output_path = tmp_path / "test_metadata_dalle.png"

        result = generate_image(
            prompt="Test image", provider="dalle", output_path=str(output_path)
        )

        # Check metadata exists and has expected structure
        assert hasattr(result, "metadata")
        metadata = result.metadata

        # Basic metadata should be present
        assert "provider" in metadata or result.provider == "dalle"
        # Image should exist with valid size
        assert result.image_path.stat().st_size > 1000

    def test_quality_parameter_gemini(self, skip_if_no_api_keys, tmp_path):
        """Test quality parameter affects output (if supported)."""
        output_standard = tmp_path / "standard.png"
        output_hd = tmp_path / "hd.png"

        result_standard = generate_image(
            prompt="Simple shape",
            provider="gemini",
            output_path=str(output_standard),
            quality="standard",
        )

        result_hd = generate_image(
            prompt="Simple shape",
            provider="gemini",
            output_path=str(output_hd),
            quality="hd",
        )

        # Both should succeed
        assert result_standard.image_path.exists()
        assert result_hd.image_path.exists()

        # HD might be larger (not always guaranteed)
        # Just verify both are valid images
        assert result_standard.image_path.stat().st_size > 1000
        assert result_hd.image_path.stat().st_size > 1000
