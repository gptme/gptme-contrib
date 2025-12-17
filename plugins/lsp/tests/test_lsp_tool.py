"""Tests for the LSP tool."""
# mypy: ignore-errors
# Tests use runtime sys.path manipulation which mypy can't resolve

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_tool_spec():
    """Test that the tool spec is correctly defined."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools.lsp_tool import tool

    assert tool.name == "lsp"
    assert "diagnostics" in tool.desc.lower()
    assert tool.execute is not None


def test_get_workspace_git_repo(tmp_path):
    """Test workspace detection in a git repo."""
    # Create a git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=str(tmp_path) + "\n")

        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

        from gptme_lsp.tools.lsp_tool import _get_workspace

        result = _get_workspace()
        assert result is not None


def test_ensure_pyright_available():
    """Test pyright availability check."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools.lsp_tool import _ensure_pyright

    # This test will pass if pyright is installed, skip if not
    result = _ensure_pyright()
    # Result depends on system state - just ensure it doesn't crash
    assert isinstance(result, bool)


def test_execute_status():
    """Test the status action."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools.lsp_tool import execute

    result = execute(
        code="",
        args=["status"],
        kwargs={},
        confirm=lambda x: True,  # Mock confirm function
    )

    assert result is not None
    assert "LSP Status" in result.content


def test_execute_no_args():
    """Test execution with no arguments shows usage."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools.lsp_tool import execute

    result = execute(
        code="",
        args=[],
        kwargs={},
        confirm=lambda x: True,
    )

    assert result is not None
    assert "Usage" in result.content


def test_execute_unknown_action():
    """Test execution with unknown action."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools.lsp_tool import execute

    result = execute(
        code="",
        args=["unknown"],
        kwargs={},
        confirm=lambda x: True,
    )

    assert result is not None
    assert "Unknown action" in result.content


def test_execute_diagnostics_missing_file(tmp_path):
    """Test diagnostics with missing file."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools.lsp_tool import execute

    result = execute(
        code="",
        args=["diagnostics", str(tmp_path / "nonexistent.py")],
        kwargs={},
        confirm=lambda x: True,
    )

    assert result is not None
    assert "not found" in result.content.lower()


def test_execute_diagnostics_unsupported_type(tmp_path):
    """Test diagnostics with unsupported file type."""
    # Create a file with unsupported extension
    test_file = tmp_path / "test.xyz"
    test_file.write_text("test content")

    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools.lsp_tool import execute

    result = execute(
        code="",
        args=["diagnostics", str(test_file)],
        kwargs={},
        confirm=lambda x: True,
    )

    assert result is not None
    assert "Unsupported" in result.content


@pytest.mark.skipif(
    subprocess.run(["which", "pyright"], capture_output=True).returncode != 0,
    reason="pyright not installed",
)
def test_execute_diagnostics_python_file(tmp_path):
    """Test diagnostics on a Python file (requires pyright)."""
    # Create a Python file with an error
    test_file = tmp_path / "test_errors.py"
    test_file.write_text('x: int = "not an int"  # type error\n')

    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools.lsp_tool import execute

    result = execute(
        code="",
        args=["diagnostics", str(test_file)],
        kwargs={},
        confirm=lambda x: True,
    )

    assert result is not None
    assert "Diagnostics" in result.content


def test_hooks_module_exists():
    """Test that hooks module can be imported."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.hooks import register

    # Just test that registration function exists and is callable
    assert callable(register)


def test_tools_module_exists():
    """Test that tools module can be imported."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    from gptme_lsp.tools import tool

    # Just test that tool spec is exported
    assert tool is not None
    assert tool.name == "lsp"
