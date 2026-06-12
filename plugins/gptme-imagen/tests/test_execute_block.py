"""Tests for the image_gen block execute function and webui preview wiring."""

from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def fake_image(tmp_path: Path) -> Path:
    """A minimal PNG file for testing."""
    png = tmp_path / "generated_test.png"
    # Minimal 1x1 white PNG (valid PNG header + IHDR + IDAT + IEND)
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a"  # PNG signature
            "0000000d49484452"  # IHDR chunk length+type
            "00000001000000010802000000906801f8"  # 1x1 RGB
            "0000000a4944415408d76360000000020001e221bc330000000049454e44ae426082"
        )
    )
    return png


def _make_image_result(path: Path):
    from gptme_imagen.tools.image_gen import ImageResult

    return ImageResult(
        provider="gemini",
        prompt="test prompt",
        image_path=path,
        metadata={"model": "imagen-3", "size": "1024x1024", "quality": "standard"},
    )


@contextmanager
def _mock_capture_and_display(stdout_content: str = ""):
    """Context manager mock for capture_and_display that returns StringIO objects."""
    stdout = StringIO(stdout_content)
    stderr = StringIO()
    # Make getvalue() return the content
    stdout.getvalue = lambda: stdout_content  # type: ignore[method-assign]
    stderr.getvalue = lambda: ""  # type: ignore[method-assign]
    yield stdout, stderr


def _run_execute(code: str, ipy_result_value, stdout_content: str = "", ipy_error=None):
    """Helper to run _execute_image_gen_block with mocked IPython."""
    from gptme_imagen.tools.image_gen import _execute_image_gen_block

    mock_ipy_result = MagicMock()
    mock_ipy_result.result = ipy_result_value
    mock_ipy_result.error_in_exec = ipy_error

    mock_ipython = MagicMock()
    mock_ipython.run_cell.return_value = mock_ipy_result

    mock_confirm = MagicMock()
    mock_confirm.action = "confirm"

    with (
        patch("gptme.hooks.get_confirmation", return_value=mock_confirm),
        patch("gptme.hooks.ConfirmAction") as mock_action_cls,
        patch("gptme.tools.python._get_ipython", return_value=mock_ipython),
        patch(
            "gptme.tools.python.capture_and_display",
            side_effect=lambda: _mock_capture_and_display(stdout_content),
        ),
    ):
        mock_action_cls.CONFIRM = "confirm"
        return list(_execute_image_gen_block(code, [], {}))


class TestExecuteImageGenBlock:
    """Tests for _execute_image_gen_block."""

    def test_execute_function_is_wired(self):
        """ToolSpec should have an execute function set."""
        from gptme_imagen.tools.image_gen import image_gen_tool

        assert image_gen_tool is not None, "image_gen_tool requires gptme installed"
        assert image_gen_tool.execute is not None, "execute must be wired into ToolSpec"

    def test_yields_message_with_image_files(self, fake_image: Path):
        """Execute function should yield a Message with files=[image_path]."""
        image_result = _make_image_result(fake_image)
        messages = _run_execute(
            "generate_image('sunset')", image_result, "Saved to: test.png"
        )

        assert len(messages) >= 1
        first = messages[0]
        assert first.files is not None, "Message needs files for inline webui preview"
        assert len(first.files) >= 1
        # The absolute path should be in the files list
        assert fake_image.absolute() in [Path(f) for f in first.files]

    def test_yields_no_files_when_no_image_result(self):
        """Execute function should yield a plain message when no ImageResult is returned."""
        messages = _run_execute("print('hello')", "some string result", "hello")

        assert len(messages) >= 1
        first = messages[0]
        assert not first.files

    def test_handles_error_gracefully(self):
        """Execute function should yield an error message on IPython execution error."""
        messages = _run_execute(
            "generate_image('test')",
            None,
            "",
            ipy_error=ValueError("API key not set"),
        )

        assert len(messages) == 1
        assert "Error" in messages[0].content

    def test_list_of_image_results(self, tmp_path: Path, fake_image: Path):
        """Execute function should handle list of ImageResults (batch generation)."""
        img2 = tmp_path / "generated_test2.png"
        img2.write_bytes(fake_image.read_bytes())

        image_results = [_make_image_result(fake_image), _make_image_result(img2)]
        messages = _run_execute(
            "batch_generate(['a','b'])", image_results, "Generated 2 images"
        )

        assert len(messages) >= 1
        first = messages[0]
        assert first.files is not None
        assert len(first.files) == 2

    def test_none_content_yields_error_message(self):
        """Execute function should handle None content gracefully."""
        from gptme_imagen.tools.image_gen import _execute_image_gen_block

        messages = list(_execute_image_gen_block(None, [], {}))
        assert len(messages) == 1
        assert "No code" in messages[0].content
