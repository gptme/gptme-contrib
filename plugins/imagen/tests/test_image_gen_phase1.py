"""Unit tests for Phase 1 enhancements to gptme_image_gen plugin."""

from unittest.mock import patch

import pytest

from gptme_image_gen.tools.image_gen import ImageResult, generate_image


class TestMultipleImageGeneration:
    """Tests for count parameter (multiple image generation)."""

    def test_count_validation_negative(self):
        """Test that count < 1 raises ValueError."""
        with pytest.raises(ValueError, match="count must be >= 1"):
            generate_image(prompt="test", provider="gemini", count=0)

    def test_count_validation_zero(self):
        """Test that count = 0 raises ValueError."""
        with pytest.raises(ValueError, match="count must be >= 1"):
            generate_image(prompt="test", provider="gemini", count=-1)

    def test_single_image_count_one(self, tmp_path):
        """Test that count=1 returns single ImageResult (backward compatible)."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=tmp_path / "generated_001.png",
                metadata={"model": "imagen-3-fast"},
            )

            result = generate_image(prompt="test", provider="gemini", count=1)

            # Should return single ImageResult, not list
            assert isinstance(result, ImageResult)
            assert not isinstance(result, list)
            assert result.provider == "gemini"

    def test_multiple_images_count_three(self, tmp_path):
        """Test that count=3 generates 3 images and returns list."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            # Mock will be called 3 times
            mock_gen.side_effect = [
                ImageResult(
                    provider="gemini",
                    prompt="test",
                    image_path=tmp_path / f"generated_{i:03d}.png",
                    metadata={"model": "imagen-3-fast"},
                )
                for i in range(1, 4)
            ]

            result = generate_image(prompt="test", provider="gemini", count=3)

            # Should return list of 3 ImageResults
            assert isinstance(result, list)
            assert len(result) == 3
            assert all(isinstance(r, ImageResult) for r in result)
            assert mock_gen.call_count == 3

    def test_multiple_images_path_numbering(self, tmp_path, monkeypatch):
        """Test that multiple images get numbered paths."""
        monkeypatch.chdir(tmp_path)

        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            # Capture the paths passed to mock
            paths_used = []

            def capture_path(prompt, size, quality, output_path, images=None):
                paths_used.append(output_path)
                return ImageResult(
                    provider="gemini",
                    prompt=prompt,
                    image_path=output_path,
                    metadata={},
                )

            mock_gen.side_effect = capture_path

            _result = generate_image(
                prompt="test", provider="gemini", count=3, output_path="test.png"
            )

            # Verify paths are numbered
            assert len(paths_used) == 3
            assert str(paths_used[0]).endswith("test_001.png")
            assert str(paths_used[1]).endswith("test_002.png")
            assert str(paths_used[2]).endswith("test_003.png")

    def test_error_handling_in_loop(self, tmp_path):
        """Test that error in generation provides context about which image failed."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            # First call succeeds, second fails
            mock_gen.side_effect = [
                ImageResult(
                    provider="gemini",
                    prompt="test",
                    image_path=tmp_path / "generated_001.png",
                    metadata={},
                ),
                ValueError("API error"),
            ]

            with pytest.raises(
                RuntimeError, match=r"Failed to generate image 2/3 with gemini"
            ):
                generate_image(prompt="test", provider="gemini", count=3)


class TestViewIntegration:
    """Tests for view parameter (vision integration)."""

    def test_view_disabled_by_default(self, tmp_path):
        """Test that view=False (default) doesn't call view_image."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=tmp_path / "test.png",
                metadata={},
            )

            with patch("gptme.tools.vision.view_image") as mock_view:
                _result = generate_image(prompt="test", provider="gemini", view=False)

                # view_image should not be called
                mock_view.assert_not_called()

    def test_view_single_image(self, tmp_path):
        """Test that view=True calls view_image for single image."""
        test_path = tmp_path / "test.png"

        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=test_path,
                metadata={},
            )

            with patch("gptme.tools.vision.view_image") as mock_view_image:
                _result = generate_image(prompt="test", provider="gemini", view=True)

                # view_image should be called once with the image path
                mock_view_image.assert_called_once_with(test_path)

    def test_view_multiple_images(self, tmp_path):
        """Test that view=True calls view_image for each generated image."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            paths = [tmp_path / f"test_{i}.png" for i in range(3)]
            mock_gen.side_effect = [
                ImageResult(
                    provider="gemini", prompt="test", image_path=path, metadata={}
                )
                for path in paths
            ]

            with patch("gptme.tools.vision.view_image") as mock_view_image:
                _result = generate_image(
                    prompt="test", provider="gemini", count=3, view=True
                )

                # view_image should be called 3 times, once for each image
                assert mock_view_image.call_count == 3
                for path in paths:
                    assert any(
                        call[0][0] == path for call in mock_view_image.call_args_list
                    )

    def test_view_graceful_fallback_import_error(self, tmp_path):
        """Test that ImportError in view_image is handled gracefully."""
        with patch("gptme_image_gen.tools.image_gen._generate_gemini") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=tmp_path / "test.png",
                metadata={},
            )

            # Simulate ImportError when trying to import view_image
            with patch(
                "gptme.tools.vision.view_image",
                side_effect=ImportError("vision tool not available"),
            ):
                # Should not raise, just skip viewing
                result = generate_image(prompt="test", provider="gemini", view=True)
                assert isinstance(result, ImageResult)


class TestExecuteFunction:
    """Tests for _execute_generate_image with new parameters."""

    def test_execute_single_image_output(self, tmp_path):
        """Test execute function formats single image output correctly."""
        from gptme_image_gen.tools.image_gen import _execute_generate_image

        with patch("gptme_image_gen.tools.image_gen.generate_image") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test prompt",
                image_path=tmp_path / "test.png",
                metadata={"model": "imagen-3-fast", "size": "1024x1024"},
            )

            output = _execute_generate_image(prompt="test prompt", provider="gemini")

            assert "=== Image Generated ===" in output
            assert "Provider: gemini" in output
            assert "Prompt: test prompt" in output
            assert "Saved to:" in output
            assert "test.png" in output

    def test_execute_multiple_images_output(self, tmp_path):
        """Test execute function formats multiple images output correctly."""
        from gptme_image_gen.tools.image_gen import _execute_generate_image

        with patch("gptme_image_gen.tools.image_gen.generate_image") as mock_gen:
            mock_gen.return_value = [
                ImageResult(
                    provider="gemini",
                    prompt="test",
                    image_path=tmp_path / f"test_{i}.png",
                    metadata={},
                )
                for i in range(3)
            ]

            output = _execute_generate_image(prompt="test", provider="gemini", count=3)

            assert "=== 3 Images Generated ===" in output
            assert "--- Image 1/3 ---" in output
            assert "--- Image 2/3 ---" in output
            assert "--- Image 3/3 ---" in output

    def test_execute_view_indicator(self, tmp_path):
        """Test that execute shows view indicator when view=True."""
        from gptme_image_gen.tools.image_gen import _execute_generate_image

        with patch("gptme_image_gen.tools.image_gen.generate_image") as mock_gen:
            mock_gen.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=tmp_path / "test.png",
                metadata={},
            )

            output = _execute_generate_image(
                prompt="test", provider="gemini", view=True
            )

            assert "✓ Images displayed to assistant for review" in output
