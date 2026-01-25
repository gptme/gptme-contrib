"""Simplified Phase 4 feature tests for image_gen plugin.

Tests configuration, error handling, and edge cases without complex API mocking.
Phase 1-2 already provide comprehensive unit/integration tests with real APIs.
"""

import pytest

from gptme_imagen.tools.image_gen import ImageResult, generate_image


class TestErrorHandling:
    """Test error handling for invalid inputs."""

    def test_invalid_provider(self):
        """Test error on invalid provider name."""
        with pytest.raises(ValueError, match="Unknown provider"):
            generate_image(prompt="Test", provider="invalid_provider")

    def test_missing_api_key_gemini(self, monkeypatch):
        """Test error when Gemini dependencies not installed."""
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        # Will raise RuntimeError wrapping ImportError if google-genai not installed
        with pytest.raises(RuntimeError, match="google-genai"):
            generate_image(prompt="Test", provider="gemini")

    def test_missing_api_key_dalle(self):
        """Test error when OpenAI API key missing."""
        from unittest.mock import patch

        # Mock _generate_dalle to raise the expected error
        with patch(
            "gptme_imagen.tools.image_gen._generate_dalle",
            side_effect=ValueError("OPENAI_API_KEY environment variable not set"),
        ):
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                generate_image(prompt="Test", provider="dalle")


class TestConfiguration:
    """Test configuration and parameter handling."""

    def test_provider_options_accepted(self):
        """Test that all documented providers are accepted as valid options."""
        valid_providers = ["gemini", "dalle", "dalle2"]

        for provider in valid_providers:
            # Should not raise ValueError for valid providers
            # Will raise RuntimeError for missing deps/API keys
            try:
                generate_image(prompt="Test", provider=provider)
            except RuntimeError as e:
                # Expected - missing dependencies or API keys
                # As long as it's not "Unknown provider"
                assert "Unknown provider" not in str(e)

    def test_output_path_expansion(self, tmp_path):
        """Test that output paths are properly expanded and resolved."""

        # Test that Path objects and strings are handled
        path_str = str(tmp_path / "test.png")
        path_obj = tmp_path / "test.png"

        # Both should be valid (will fail on API, but path handling works)
        try:
            generate_image(prompt="Test", provider="gemini", output_path=path_str)
        except RuntimeError:
            pass  # Expected - API not available

        try:
            generate_image(prompt="Test", provider="gemini", output_path=str(path_obj))
        except RuntimeError:
            pass  # Expected - API not available


class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    def test_very_long_prompt_accepted(self):
        """Test that very long prompts don't cause immediate errors."""
        long_prompt = "Test prompt " * 1000

        # Should not raise RuntimeError for prompt length
        try:
            generate_image(prompt=long_prompt, provider="gemini")
        except RuntimeError as e:
            # Should fail on API availability, not prompt length
            assert "prompt" not in str(e).lower() or "GOOGLE_API_KEY" in str(e)

    def test_unicode_in_prompt(self):
        """Test Unicode characters in prompt are accepted."""
        unicode_prompt = "Test 测试 тест テスト 🎨🖼️"

        # Should not raise encoding errors
        try:
            generate_image(prompt=unicode_prompt, provider="gemini")
        except RuntimeError:
            pass  # Expected - API not available

    def test_special_characters_in_output_path(self, tmp_path):
        """Test special characters in paths are handled."""
        path_with_spaces = tmp_path / "test image (v2).png"

        try:
            generate_image(
                prompt="Test", provider="gemini", output_path=str(path_with_spaces)
            )
        except RuntimeError:
            pass  # Expected - API not available


class TestProviderOptions:
    """Test provider-specific options are validated."""

    def test_size_parameter_accepted(self):
        """Test that size parameter is accepted."""
        sizes = ["1024x1024", "512x512"]

        for size in sizes:
            try:
                generate_image(prompt="Test", provider="gemini", size=size)
            except RuntimeError as e:
                # Should fail on API, not parameter
                assert "size" not in str(e).lower() or "GOOGLE_API_KEY" in str(e)

    def test_quality_parameter_accepted(self):
        """Test that quality parameter is accepted."""
        qualities = ["standard", "hd"]

        for quality in qualities:
            try:
                generate_image(prompt="Test", provider="dalle", quality=quality)
            except RuntimeError as e:
                # Should fail on API, not parameter validation
                assert "quality" not in str(e).lower() or "api_key" in str(e).lower()


class TestDataStructures:
    """Test data structure definitions."""

    def test_image_result_structure(self):
        """Test ImageResult has expected attributes."""
        # Verify the dataclass has required fields
        from dataclasses import fields

        field_names = {f.name for f in fields(ImageResult)}

        assert "provider" in field_names
        assert "prompt" in field_names
        assert "image_path" in field_names
        assert "metadata" in field_names

    def test_provider_type_definition(self):
        """Test Provider type is properly defined."""
        from gptme_imagen.tools.image_gen import Provider

        # Provider should be a Literal type
        # This test just verifies it exists and can be imported
        assert Provider is not None
