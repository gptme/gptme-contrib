"""Tests for AUTOMATIC1111 image generation provider."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from gptme_imagen.tools.image_gen import ImageResult, _generate_a1111, generate_image


def _make_mock_response(img_bytes: bytes = b"\x89PNG\r\n") -> MagicMock:
    """Return a mock requests.Response with base64-encoded image data."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"images": [base64.b64encode(img_bytes).decode()]}
    mock_resp.raise_for_status.return_value = None
    return mock_resp


class TestGenerateA1111:
    def test_basic_generation(self, tmp_path: Path) -> None:
        """_generate_a1111 saves the decoded image and returns an ImageResult."""
        output_path = tmp_path / "out.png"
        img_bytes = b"\x89PNG\r\n\x1a\n"

        with patch(
            "requests.post", return_value=_make_mock_response(img_bytes)
        ) as mock_post:
            result = _generate_a1111("a red square", "512x512", output_path)

        assert isinstance(result, ImageResult)
        assert result.provider == "a1111"
        assert result.image_path == output_path
        assert output_path.read_bytes() == img_bytes

        call_kwargs = mock_post.call_args
        assert "sdapi/v1/txt2img" in call_kwargs[0][0]
        payload = call_kwargs[1]["json"]
        assert payload["prompt"] == "a red square"
        assert payload["width"] == 512
        assert payload["height"] == 512

    def test_default_base_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uses localhost:7860 when env var is not set."""
        monkeypatch.delenv("AUTOMATIC1111_BASE_URL", raising=False)
        output_path = tmp_path / "out.png"

        with patch("requests.post", return_value=_make_mock_response()) as mock_post:
            _generate_a1111("test", "512x512", output_path)

        url = mock_post.call_args[0][0]
        assert "localhost:7860" in url

    def test_custom_base_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Respects AUTOMATIC1111_BASE_URL env var."""
        monkeypatch.setenv("AUTOMATIC1111_BASE_URL", "http://myserver:8888")
        output_path = tmp_path / "out.png"

        with patch("requests.post", return_value=_make_mock_response()) as mock_post:
            _generate_a1111("test", "512x512", output_path)

        url = mock_post.call_args[0][0]
        assert "myserver:8888" in url

    def test_connection_error_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Connection error raises RuntimeError with helpful message."""
        import sys

        output_path = tmp_path / "out.png"

        # conftest.py pre-populates sys.modules["requests"] with a MagicMock.
        # We configure that mock directly: make ConnectionError a real exception
        # class so the except clause in _generate_a1111 works correctly.
        requests_mock = sys.modules["requests"]
        monkeypatch.setattr(
            requests_mock, "post", MagicMock(side_effect=ConnectionError("refused"))
        )
        monkeypatch.setattr(
            requests_mock.exceptions, "ConnectionError", ConnectionError
        )

        with pytest.raises(RuntimeError, match="Could not connect to AUTOMATIC1111"):
            _generate_a1111("test", "512x512", output_path)

    def test_via_generate_image(self, tmp_path: Path) -> None:
        """generate_image dispatches to _generate_a1111 for provider='a1111'."""
        output_path = tmp_path / "via_dispatch.png"

        with patch(
            "gptme_imagen.tools.image_gen._generate_a1111",
            return_value=ImageResult(
                provider="a1111",
                prompt="test",
                image_path=output_path,
                metadata={},
            ),
        ) as mock_a1111:
            result = generate_image(
                "test prompt",
                provider="a1111",
                output_path=str(output_path),
                show_progress=False,
            )

        mock_a1111.assert_called_once()
        assert isinstance(result, ImageResult)
        assert result.provider == "a1111"
