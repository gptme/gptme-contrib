"""Post-save hook for automatic LSP diagnostics.

Automatically runs diagnostics after saving files using proper LSP protocol,
helping catch errors immediately after edits.
"""

import logging
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.hooks import HookType, register_hook
from gptme.message import Message

if TYPE_CHECKING:
    from gptme.logmanager import Log

logger = logging.getLogger(__name__)

# Supported file extensions for auto-diagnostics (matches what LSP supports)
SUPPORTED_EXTENSIONS = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}


def _get_quick_lsp_diagnostics(file: Path, workspace: Path | None = None) -> str | None:
    """Get diagnostics using LSP protocol for quick post-save feedback.

    Uses the generic LSP infrastructure which works with any language server.
    Returns brief error summary or None if no errors.
    """
    # Import here to avoid circular imports at module load time
    from ..tools.lsp_tool import _get_lsp_diagnostics

    result = _get_lsp_diagnostics(file, workspace)

    if result is None:
        # No LSP server available for this file type, skip silently
        return None

    # Check if there are actual errors (not just "no errors found" message)
    if "✅" in result or "No errors" in result.lower():
        return None

    # Return a brief version for post-save feedback
    # Limit to first few lines to keep feedback concise
    lines = result.strip().split("\n")
    if len(lines) > 5:
        return "\n".join(lines[:5]) + f"\n... ({len(lines) - 5} more lines)"

    return result


def post_save_diagnostics_hook(
    log: "Log | None",
    workspace: Path | None,
    path: Path,
    content: str,
    created: bool,
) -> Generator[Message, None, None]:
    """Hook that runs after file save operations.

    Automatically checks saved files for errors using LSP-powered diagnostics.
    Only reports errors (not warnings) to keep output focused.

    Args:
        log: The conversation log (may be None)
        workspace: Workspace directory path
        path: Path to the file that was saved
        content: Content that was saved
        created: Whether file was newly created (vs overwritten)
    """
    file = path

    # Only check supported file types
    if file.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return

    # Make absolute if relative
    if not file.is_absolute() and workspace:
        file = workspace / file

    if not file.exists():
        return

    logger.debug(f"Running post-save diagnostics on {file}")

    # Run diagnostics using LSP protocol
    result = _get_quick_lsp_diagnostics(file, workspace)

    if result:
        yield Message(
            "system",
            f"⚡ **Auto-diagnostics** for `{file.name}`:\n{result}\n\n"
            "_Use `lsp diagnostics {file.name}` for full details._",
        )


def register() -> None:
    """Register LSP hooks with gptme."""
    logger.info("LSP plugin: Registering hooks")

    # Register post-save hook for FILE_POST_SAVE (covers save tool)
    register_hook(
        name="lsp.post_save_diagnostics",
        hook_type=HookType.FILE_POST_SAVE,
        func=post_save_diagnostics_hook,
        priority=0,  # Default priority
    )

    # Note: FILE_POST_PATCH has a different signature, so we'd need a separate
    # hook function for patch operations if needed. For now, focusing on saves.

    logger.info("LSP plugin: Hooks registered")
