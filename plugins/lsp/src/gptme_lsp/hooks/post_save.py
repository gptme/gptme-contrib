"""Post-save hook for automatic LSP diagnostics.

Automatically runs diagnostics after saving Python or TypeScript files,
helping catch errors immediately after edits.
"""

import logging
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.hooks import HookType, register_hook
from gptme.message import Message

if TYPE_CHECKING:
    from gptme.logmanager import Log
    from gptme.tools.base import ToolUse

logger = logging.getLogger(__name__)

# Supported file extensions for auto-diagnostics
SUPPORTED_EXTENSIONS = {".py", ".pyi", ".ts", ".tsx"}


def _run_quick_diagnostics(file: Path, workspace: Path | None = None) -> str | None:
    """Run quick diagnostics on a file. Returns formatted result or None if no issues."""
    suffix = file.suffix.lower()

    if suffix in (".py", ".pyi"):
        return _check_python(file, workspace)
    elif suffix in (".ts", ".tsx"):
        return _check_typescript(file, workspace)

    return None


def _check_python(file: Path, workspace: Path | None = None) -> str | None:
    """Run pyright on a Python file."""
    try:
        # Quick check if pyright is available
        which_result = subprocess.run(
            ["which", "pyright"],
            capture_output=True,
            timeout=5,
        )
        if which_result.returncode != 0:
            return None  # pyright not available, skip silently

        # Run pyright
        cmd = ["pyright", "--outputjson", str(file)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=30,  # Quick timeout for post-save hook
        )

        import json

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        diagnostics = data.get("generalDiagnostics", [])
        if not diagnostics:
            return None  # No issues found

        # Count errors only (skip warnings for post-save hook)
        errors = [d for d in diagnostics if d.get("severity") == "error"]
        if not errors:
            return None  # Only warnings, skip for now

        # Format brief summary
        lines = []
        for diag in errors[:3]:  # Limit to 3 errors
            range_info = diag.get("range", {})
            start = range_info.get("start", {})
            line = start.get("line", 0) + 1
            message = diag.get("message", "Unknown error")
            lines.append(f"  Line {line}: {message}")

        extra = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        return f"❌ {len(errors)} error(s) found{extra}:\n" + "\n".join(lines)

    except subprocess.TimeoutExpired:
        return None  # Timeout, skip
    except Exception as e:
        logger.debug(f"Python diagnostics error: {e}")
        return None


def _check_typescript(file: Path, workspace: Path | None = None) -> str | None:
    """Run quick TypeScript check."""
    try:
        # Quick type check with tsc
        cmd = ["npx", "--yes", "tsc", "--noEmit", "--pretty", "false", str(file)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=30,
        )

        if result.returncode == 0:
            return None  # No errors

        output = (result.stdout + result.stderr).strip()
        if not output:
            return None

        # Count errors
        error_lines = [line for line in output.split("\n") if ": error TS" in line]
        if not error_lines:
            return None

        # Format brief summary
        summary_lines = []
        for line in error_lines[:3]:
            summary_lines.append(f"  {line}")

        extra = f" (+{len(error_lines) - 3} more)" if len(error_lines) > 3 else ""
        return f"❌ {len(error_lines)} TypeScript error(s){extra}:\n" + "\n".join(
            summary_lines
        )

    except subprocess.TimeoutExpired:
        return None
    except Exception as e:
        logger.debug(f"TypeScript diagnostics error: {e}")
        return None


def post_save_diagnostics_hook(
    log: "Log",
    workspace: Path | None,
    tool_use: "ToolUse",
) -> Generator[Message, None, None]:
    """Hook that runs after file save operations.

    Automatically checks saved files for errors using LSP-powered diagnostics.
    Only reports errors (not warnings) to keep output focused.

    Args:
        log: The conversation log
        workspace: Workspace directory path
        tool_use: The tool that just executed (save or patch)
    """
    # Only trigger for save and patch tools
    if tool_use.tool not in ("save", "patch"):
        return

    # Extract file path from tool args
    file_path: str | None = None

    if tool_use.tool == "save":
        # save tool has path as first arg
        if tool_use.args:
            file_path = tool_use.args[0]
    elif tool_use.tool == "patch":
        # patch tool also has path as first arg
        if tool_use.args:
            file_path = tool_use.args[0]

    if not file_path:
        return

    file = Path(file_path)

    # Only check supported file types
    if file.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return

    # Make absolute if relative
    if not file.is_absolute() and workspace:
        file = workspace / file

    if not file.exists():
        return

    logger.debug(f"Running post-save diagnostics on {file}")

    # Run diagnostics
    result = _run_quick_diagnostics(file, workspace)

    if result:
        yield Message(
            "system",
            f"⚡ **Auto-diagnostics** for `{file.name}`:\n{result}\n\n"
            "_Use `lsp diagnostics {file.name}` for full details._",
        )


def register() -> None:
    """Register LSP hooks with gptme."""
    logger.info("LSP plugin: Registering hooks")

    # Register post-save hook
    register_hook(
        name="lsp.post_save_diagnostics",
        hook_type=HookType.TOOL_POST_EXECUTE,
        func=post_save_diagnostics_hook,
        priority=0,  # Default priority
    )

    logger.info("LSP plugin: Hooks registered")
