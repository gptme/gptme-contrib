"""Tests for the LSP tool."""
# mypy: ignore-errors
# Tests use runtime sys.path manipulation which mypy can't resolve

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add plugin source to path once at module load
_plugin_src = str(Path(__file__).parent.parent / "src")
if _plugin_src not in sys.path:
    sys.path.insert(0, _plugin_src)

# Now imports can happen at module level (path setup required first)
from gptme_lsp.hooks import register  # noqa: E402
from gptme_lsp.lsp_client import TextEdit, WorkspaceEdit  # noqa: E402
from gptme_lsp.tools import tool  # noqa: E402
from gptme_lsp.tools.lsp_tool import _get_workspace, execute  # noqa: E402


def test_tool_spec():
    """Test that the tool spec is correctly defined."""
    assert tool.name == "lsp"
    assert "lsp" in tool.desc.lower() or "language server" in tool.desc.lower()
    assert tool.execute is not None


def test_get_workspace_git_repo(tmp_path):
    """Test workspace detection in a git repo."""
    # Create a git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=str(tmp_path) + "\n")
        result = _get_workspace()
        assert result is not None


def test_server_availability():
    """Test LSP server availability check."""
    # This test checks if LSP server detection works
    # The result depends on system state - just ensure it doesn't crash
    from gptme_lsp.lsp_client import KNOWN_SERVERS

    assert isinstance(KNOWN_SERVERS, dict)
    assert "python" in KNOWN_SERVERS


def test_execute_status():
    """Test the status action."""
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
    result = execute(
        code="",
        args=["unknown"],
        kwargs={},
        confirm=lambda x: True,
    )

    assert result is not None
    assert "Unknown action" in result.content


def test_execute_rename_previews_workspace_edit(tmp_path):
    """Test rename action formats LSP workspace edit previews."""
    test_file = tmp_path / "module.py"
    test_file.write_text("def old_name():\n    return old_name()\n")
    edit = TextEdit(
        file=test_file,
        start_line=1,
        start_column=5,
        end_line=1,
        end_column=13,
        new_text="new_name",
    )

    with patch("gptme_lsp.tools.lsp_tool._get_workspace", return_value=tmp_path):
        with patch(
            "gptme_lsp.tools.lsp_tool._get_lsp_rename",
            return_value=WorkspaceEdit([edit]),
        ) as mock_rename:
            result = execute(
                code="",
                args=["rename", f"{test_file}:1:5", "new_name"],
                kwargs={},
                confirm=lambda x: True,
            )

    assert result is not None
    assert "Rename to 'new_name'" in result.content
    assert "1 edit(s) in 1 file(s)" in result.content
    assert "**module.py**" in result.content
    assert "Line 1:5-1:13" in result.content
    mock_rename.assert_called_once_with(test_file, 1, 5, "new_name", tmp_path)


def test_execute_diagnostics_missing_file(tmp_path):
    """Test diagnostics with missing file."""
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

    result = execute(
        code="",
        args=["diagnostics", str(test_file)],
        kwargs={},
        confirm=lambda x: True,
    )

    assert result is not None
    assert "Unsupported" in result.content


@pytest.mark.skipif(
    shutil.which("pyright") is None,
    reason="pyright not installed",
)
def test_execute_diagnostics_python_file(tmp_path):
    """Test diagnostics on a Python file (requires pyright)."""
    # Create a Python file with an error
    test_file = tmp_path / "test_errors.py"
    test_file.write_text('x: int = "not an int"  # type error\n')

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
    # Just test that registration function exists and is callable
    assert callable(register)


def test_tools_module_exists():
    """Test that tools module can be imported."""
    # Just test that tool spec is exported
    assert tool is not None
    assert tool.name == "lsp"


def test_tool_has_command():
    """Test that the tool has the /lsp command registered."""
    assert "lsp" in tool.commands
    assert callable(tool.commands["lsp"])


def test_lsp_command_returns_generator():
    """Test that the lsp command returns a generator."""
    from unittest.mock import MagicMock

    from gptme_lsp.tools.lsp_tool import _lsp_command

    # Create a mock CommandContext
    mock_ctx = MagicMock()
    mock_ctx.args = ["status"]
    mock_ctx.confirm = lambda *args, **kwargs: True

    # Call the command and ensure it's a generator
    result = _lsp_command(mock_ctx)
    import types

    assert isinstance(result, types.GeneratorType)

    # Consume the generator
    messages = list(result)
    assert len(messages) == 1
    assert "LSP Status" in messages[0].content
