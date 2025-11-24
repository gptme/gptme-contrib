"""Unit tests for Phase 2 enhancements to gptme_image_gen plugin."""

from unittest.mock import patch

import pytest

from gptme_image_gen.tools.image_gen import (
    STYLE_PRESETS,
    ImageResult,
    _enhance_prompt,
    generate_image,
)


class TestStylePresets:
    """Tests for style preset functionality."""

    def test_style_preset_applied_to_prompt(self, tmp_path):
        """Test that style preset modifies prompt."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            # Capture the actual prompt passed to generator
            captured_prompt = None

            def capture_prompt(prompt, size, quality, output_path):
                nonlocal captured_prompt
                captured_prompt = prompt
                return ImageResult(
                    provider="gemini",
                    prompt=prompt,
                    image_path=tmp_path / "test.png",
                    metadata={},
                )

            mock_gen.side_effect = capture_prompt

            original_prompt = "office workspace"
            generate_image(
                prompt=original_prompt,
                provider="gemini",
                style="technical-diagram",
                output_path=str(tmp_path / "test.png"),
            )

            # Prompt should include style description
            assert captured_prompt is not None
            assert original_prompt in captured_prompt
            assert "Style:" in captured_prompt
            assert "technical" in captured_prompt.lower()

    def test_all_style_presets_have_descriptions(self):
        """Test that all style literals have preset descriptions."""
        # Get style literals from type hint
        from typing import get_args

        from gptme_image_gen.tools.image_gen import Style

        style_literals = get_args(Style)

        for style in style_literals:
            assert style in STYLE_PRESETS, f"Missing preset for style: {style}"
            assert STYLE_PRESETS[style], f"Empty description for style: {style}"
            assert len(STYLE_PRESETS[style]) > 10, f"Too short description: {style}"

    def test_no_style_leaves_prompt_unchanged(self, tmp_path):
        """Test that prompt is unchanged when no style specified."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            captured_prompt = None

            def capture_prompt(prompt, size, quality, output_path):
                nonlocal captured_prompt
                captured_prompt = prompt
                return ImageResult(
                    provider="gemini",
                    prompt=prompt,
                    image_path=tmp_path / "test.png",
                    metadata={},
                )

            mock_gen.side_effect = capture_prompt

            original_prompt = "test image"
            generate_image(
                prompt=original_prompt,
                provider="gemini",
                style=None,  # No style
                output_path=str(tmp_path / "test.png"),
            )

            # Prompt should be unchanged
            assert captured_prompt == original_prompt


class TestPromptEnhancement:
    """Tests for prompt enhancement functionality."""

    def test_enhance_adds_quality_keywords(self):
        """Test that enhancement adds quality descriptors."""
        simple_prompt = "a house"
        enhanced = _enhance_prompt(simple_prompt)

        assert simple_prompt in enhanced
        assert any(
            kw in enhanced.lower()
            for kw in ["quality", "detailed", "professional", "composed"]
        )

    def test_enhance_short_prompt_adds_composition(self):
        """Test that short prompts get composition guidance."""
        short_prompt = "logo"  # Very short
        enhanced = _enhance_prompt(short_prompt)

        assert "well-composed" in enhanced or "clear focus" in enhanced

    def test_enhance_existing_quality_keywords_not_duplicated(self):
        """Test that enhancement doesn't duplicate quality keywords."""
        prompt_with_quality = "high quality detailed professional artwork"
        enhanced = _enhance_prompt(prompt_with_quality)

        # Should not add duplicate quality keywords
        assert enhanced.count("high quality") == 1
        assert enhanced.count("detailed") == 1
        assert enhanced.count("professional") == 1

    def test_enhance_parameter_integration(self, tmp_path):
        """Test that enhance=True triggers prompt enhancement."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            captured_prompt = None

            def capture_prompt(prompt, size, quality, output_path):
                nonlocal captured_prompt
                captured_prompt = prompt
                return ImageResult(
                    provider="gemini",
                    prompt=prompt,
                    image_path=tmp_path / "test.png",
                    metadata={},
                )

            mock_gen.side_effect = capture_prompt

            original_prompt = "simple logo"
            generate_image(
                prompt=original_prompt,
                provider="gemini",
                enhance=True,
                output_path=str(tmp_path / "test.png"),
            )

            # Prompt should be enhanced
            assert captured_prompt is not None
            assert len(captured_prompt) > len(original_prompt)
            assert any(kw in captured_prompt.lower() for kw in ["quality", "detailed"])


class TestProgressIndicators:
    """Tests for progress indicator functionality."""

    def test_progress_shown_for_single_image(self, tmp_path, capsys):
        """Test progress indicator shown for single image."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=tmp_path / "test.png",
                metadata={},
            )

            generate_image(
                prompt="test",
                provider="gemini",
                show_progress=True,
                output_path=str(tmp_path / "test.png"),
            )

            captured = capsys.readouterr()
            assert "🎨 Generating image with gemini" in captured.out
            assert "✅ Image generated successfully" in captured.out

    def test_progress_shown_for_multiple_images(self, tmp_path, capsys):
        """Test progress indicators for multiple images."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.side_effect = [
                ImageResult(
                    provider="gemini",
                    prompt="test",
                    image_path=tmp_path / f"test_{i}.png",
                    metadata={},
                )
                for i in range(3)
            ]

            generate_image(
                prompt="test",
                provider="gemini",
                count=3,
                show_progress=True,
                output_path=str(tmp_path / "test.png"),
            )

            captured = capsys.readouterr()
            assert "🎨 Generating 3 images with gemini" in captured.out
            assert "→ Image 1/3" in captured.out
            assert "→ Image 2/3" in captured.out
            assert "→ Image 3/3" in captured.out
            assert "✅ Generated 3/3 images successfully" in captured.out
            assert captured.out.count("✓") == 3

    def test_progress_disabled(self, tmp_path, capsys):
        """Test that show_progress=False disables indicators."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=tmp_path / "test.png",
                metadata={},
            )

            generate_image(
                prompt="test",
                provider="gemini",
                show_progress=False,
                output_path=str(tmp_path / "test.png"),
            )

            captured = capsys.readouterr()
            assert "🎨" not in captured.out
            assert "✅" not in captured.out

    def test_progress_with_error(self, tmp_path, capsys):
        """Test progress indicators when generation fails."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.side_effect = [
                ImageResult(
                    provider="gemini",
                    prompt="test",
                    image_path=tmp_path / "test_1.png",
                    metadata={},
                ),
                ValueError("API error"),
            ]

            with pytest.raises(RuntimeError):
                generate_image(
                    prompt="test",
                    provider="gemini",
                    count=2,
                    show_progress=True,
                    output_path=str(tmp_path / "test.png"),
                )

            captured = capsys.readouterr()
            assert "→ Image 1/2... ✓" in captured.out
            assert "→ Image 2/2... ✗" in captured.out


class TestEnhancedErrorMessages:
    """Tests for enhanced error message functionality."""

    def test_api_key_error_message(self, tmp_path):
        """Test that API key errors get helpful messages."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.side_effect = ValueError("API key not found")

            with pytest.raises(RuntimeError) as exc_info:
                generate_image(
                    prompt="test",
                    provider="gemini",
                    output_path=str(tmp_path / "test.png"),
                )

            error_message = str(exc_info.value)
            assert "Missing or invalid API key" in error_message
            assert "GEMINI_API_KEY" in error_message

    def test_quota_error_message(self, tmp_path):
        """Test that quota errors get helpful messages."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.side_effect = ValueError("Quota exceeded")

            with pytest.raises(RuntimeError) as exc_info:
                generate_image(
                    prompt="test",
                    provider="gemini",
                    output_path=str(tmp_path / "test.png"),
                )

            error_message = str(exc_info.value)
            assert "quota or rate limit" in error_message.lower()
            assert "Wait a moment" in error_message

    def test_network_error_message(self, tmp_path):
        """Test that network errors get helpful messages."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.side_effect = ValueError("Network connection failed")

            with pytest.raises(RuntimeError) as exc_info:
                generate_image(
                    prompt="test",
                    provider="gemini",
                    output_path=str(tmp_path / "test.png"),
                )

            error_message = str(exc_info.value)
            assert "Network connection issue" in error_message
            assert "Check your internet connection" in error_message


class TestCombinedPhase2Features:
    """Tests for combining multiple Phase 2 features."""

    def test_style_and_enhance_together(self, tmp_path):
        """Test that style and enhance work together."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            captured_prompt = None

            def capture_prompt(prompt, size, quality, output_path):
                nonlocal captured_prompt
                captured_prompt = prompt
                return ImageResult(
                    provider="gemini",
                    prompt=prompt,
                    image_path=tmp_path / "test.png",
                    metadata={},
                )

            mock_gen.side_effect = capture_prompt

            original_prompt = "diagram"
            generate_image(
                prompt=original_prompt,
                provider="gemini",
                style="technical-diagram",
                enhance=True,
                output_path=str(tmp_path / "test.png"),
            )

            # Should have both style and enhancement
            assert captured_prompt is not None
            assert "Style:" in captured_prompt  # Style applied
            assert (
                len(captured_prompt) > len(original_prompt) + 50
            )  # Enhanced (style desc + quality keywords)

    def test_all_phase2_features_with_phase1_features(self, tmp_path):
        """Test Phase 2 features work with Phase 1 count and view."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            with patch("gptme_image_gen.tools.image_gen.view_image") as mock_view:
                mock_gen.side_effect = [
                    ImageResult(
                        provider="gemini",
                        prompt="test",
                        image_path=tmp_path / f"test_{i}.png",
                        metadata={},
                    )
                    for i in range(3)
                ]

                result = generate_image(
                    prompt="illustration",
                    provider="gemini",
                    style="watercolor",
                    enhance=True,
                    count=3,  # Phase 1
                    view=True,  # Phase 1
                    output_path=str(tmp_path / "test.png"),
                )

                # Phase 1 features work
                assert isinstance(result, list)
                assert len(result) == 3
                assert mock_view.call_count == 3  # view_image called for each

                # Phase 2 features applied (check first call)
                first_call_prompt = mock_gen.call_args_list[0][0][0]
                assert "Style:" in first_call_prompt  # Style
                assert any(
                    kw in first_call_prompt.lower()
                    for kw in ["quality", "detailed", "professional"]
                )  # Enhanced
