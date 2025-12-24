"""Unit tests for image modification feature."""

from pathlib import Path
from unittest.mock import patch

import pytest

from gptme_image_gen.tools.image_gen import ImageResult, modify_image


class TestModifyImage:
    """Tests for modify_image function."""

    def test_modify_image_basic(self, tmp_path):
        """Test basic image modification."""
        # Create a test input image
        input_path = tmp_path / "input.png"
        input_path.write_bytes(b"fake image data")

        output_path = tmp_path / "output.png"

        with patch("gptme_image_gen.tools.image_gen._modify_gemini") as mock_modify:
            mock_modify.return_value = ImageResult(
                provider="gemini",
                prompt="make it blue",
                image_path=output_path,
                metadata={"type": "modification"},
            )

            result = modify_image(
                image_path=str(input_path),
                prompt="make it blue",
                output_path=str(output_path),
                show_progress=False,
            )

            assert result.provider == "gemini"
            assert result.prompt == "make it blue"
            assert result.image_path == output_path
            mock_modify.assert_called_once()

    def test_modify_image_file_not_found(self, tmp_path):
        """Test error when input image doesn't exist."""
        with pytest.raises(FileNotFoundError, match="Image not found"):
            modify_image(
                image_path=str(tmp_path / "nonexistent.png"),
                prompt="test",
            )

    def test_modify_image_unsupported_provider(self, tmp_path):
        """Test error for unsupported providers."""
        input_path = tmp_path / "input.png"
        input_path.write_bytes(b"fake image data")

        with pytest.raises(ValueError, match="only supported for gemini"):
            modify_image(
                image_path=str(input_path),
                prompt="test",
                provider="dalle",
            )

    def test_modify_image_default_output_path(self, tmp_path):
        """Test default output path generation."""
        input_path = tmp_path / "input.png"
        input_path.write_bytes(b"fake image data")

        with patch("gptme_image_gen.tools.image_gen._modify_gemini") as mock_modify:
            mock_modify.return_value = ImageResult(
                provider="gemini",
                prompt="test",
                image_path=Path("modified_20241224_080500.png"),
                metadata={},
            )

            modify_image(
                image_path=str(input_path),
                prompt="test",
                show_progress=False,
            )

            # Should generate output path automatically
            call_args = mock_modify.call_args
            assert call_args is not None
            # The output_path argument should be a resolved path
            output_path_arg = call_args[0][2]  # Third positional argument
            assert "modified_" in str(output_path_arg)

    def test_modify_image_view_integration(self, tmp_path):
        """Test image modification with view=True displays result."""
        input_path = tmp_path / "input.png"
        input_path.write_bytes(b"fake image data")
        expected_output = tmp_path / "output.png"

        with patch("gptme_image_gen.tools.image_gen._modify_gemini") as mock_modify:
            with patch("gptme.tools.vision.view_image") as mock_view:
                mock_modify.return_value = ImageResult(
                    provider="gemini",
                    prompt="test",
                    image_path=expected_output,
                    metadata={},
                )

                modify_image(
                    image_path=str(input_path),
                    prompt="test",
                    output_path=str(expected_output),
                    view=True,
                    show_progress=False,
                )

                # View should be called once
                mock_view.assert_called_once_with(expected_output)


class TestModifyGemini:
    """Tests for _modify_gemini internal function."""

    def test_modify_gemini_documents_api_contract(self, tmp_path):
        """Document the expected API contract for _modify_gemini."""
        # This test documents the expected behavior without full API mocking
        # Integration tests would verify actual API calls

        input_path = tmp_path / "input.png"

        # Create a simple PNG file (minimal valid PNG header)
        png_data = bytes(
            [
                0x89,
                0x50,
                0x4E,
                0x47,
                0x0D,
                0x0A,
                0x1A,
                0x0A,  # PNG signature
                0x00,
                0x00,
                0x00,
                0x0D,  # IHDR length
                0x49,
                0x48,
                0x44,
                0x52,  # IHDR type
                0x00,
                0x00,
                0x00,
                0x01,  # width: 1
                0x00,
                0x00,
                0x00,
                0x01,  # height: 1
                0x08,
                0x02,  # bit depth, color type
                0x00,
                0x00,
                0x00,  # compression, filter, interlace
                0x90,
                0x77,
                0x53,
                0xDE,  # CRC
                0x00,
                0x00,
                0x00,
                0x0C,  # IDAT length
                0x49,
                0x44,
                0x41,
                0x54,  # IDAT type
                0x08,
                0xD7,
                0x63,
                0xF8,
                0xFF,
                0xFF,
                0xFF,
                0x00,  # compressed data
                0x05,
                0xFE,
                0x02,
                0xFE,  # CRC
                0x00,
                0x00,
                0x00,
                0x00,  # IEND length
                0x49,
                0x45,
                0x4E,
                0x44,  # IEND type
                0xAE,
                0x42,
                0x60,
                0x82,  # CRC
            ]
        )
        input_path.write_bytes(png_data)

        # Verify the function exists and has correct signature
        from gptme_image_gen.tools.image_gen import _modify_gemini
        import inspect

        sig = inspect.signature(_modify_gemini)
        params = list(sig.parameters.keys())

        assert "image_path" in params
        assert "prompt" in params
        assert "output_path" in params
