"""LSP tool for gptme - provides code intelligence via Language Server Protocol.

This tool provides access to LSP features:
- diagnostics: Get errors/warnings for a file
- definition: Jump to definition (Phase 2.1)
- references: Find all references (Phase 2.1)
- hover: Get documentation (Phase 2.1)
- rename: Rename symbols across project (Phase 2.2)
- format: Format document using LSP (Phase 4)
- signature: Get function signature help (Phase 4)

Uses proper LSP protocol to communicate with language servers generically,
supporting pyright (Python), typescript-language-server (JS/TS), gopls (Go),
rust-analyzer (Rust), and any other LSP-compliant server.
"""

import logging
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.message import Message
from gptme.tools.base import ConfirmFunc, Parameter, ToolSpec

from ..lsp_client import (
    LSPServer,
    KNOWN_SERVERS,
    Location,
    HoverInfo,
    WorkspaceEdit,
    SignatureInfo,
    TextEdit,
)

if TYPE_CHECKING:
    from gptme.commands import CommandContext
    from gptme.tools.base import ConfirmFunc  # noqa: F811

logger = logging.getLogger(__name__)

# Extension to language mapping for LSP server selection
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
}

# Global server instances (per workspace/language)
_servers: dict[str, LSPServer] = {}


def _get_workspace() -> Path | None:
    """Get the current workspace directory."""
    # Try to find git root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass

    # Fall back to current directory
    return Path.cwd()


def _get_or_start_server(language: str, workspace: Path) -> LSPServer | None:
    """Get or start an LSP server for the given language.

    Uses proper LSP protocol for generic language server support.
    """
    global _servers
    key = f"{workspace}:{language}"

    if key in _servers:
        server = _servers[key]
        if server.process and server.process.poll() is None:
            return server
        # Server died, remove and restart
        del _servers[key]

    # Check if server command is known and available
    if language not in KNOWN_SERVERS:
        logger.debug(f"No known LSP server for {language}")
        return None

    command = KNOWN_SERVERS[language]
    server_binary = command[0]

    # Check if server is available
    if not shutil.which(server_binary):
        logger.debug(f"LSP server not found: {server_binary}")
        return None

    # Start the server
    server = LSPServer(name=server_binary, command=command, workspace=workspace)
    if server.start():
        _servers[key] = server
        return server
    return None


def _get_lsp_diagnostics(file: Path, workspace: Path | None = None) -> str | None:
    """Get diagnostics using proper LSP protocol.

    Works with any LSP-compliant server (pyright, typescript-language-server, etc.)
    Returns formatted diagnostics string, or None if LSP not available.
    """

    # Determine language from file extension
    suffix = file.suffix.lower()
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if not language:
        return None

    if workspace is None:
        workspace = _get_workspace() or file.parent

    # Get or start server
    server = _get_or_start_server(language, workspace)
    if server is None:
        return None

    # Get diagnostics
    diagnostics = server.get_diagnostics(file)

    if not diagnostics:
        return "✅ No errors or warnings found."

    # Format diagnostics
    lines = []
    error_count = sum(1 for d in diagnostics if d.severity == "error")
    warning_count = sum(1 for d in diagnostics if d.severity == "warning")
    info_count = len(diagnostics) - error_count - warning_count

    for diag in diagnostics:
        sev_emoji = {"error": "❌", "warning": "⚠️", "info": "ℹ️", "hint": "💡"}.get(
            diag.severity, "ℹ️"
        )
        lines.append(f"{sev_emoji} Line {diag.line}: {diag.message}")
        if diag.code:
            lines[-1] += f" ({diag.code})"

    summary = []
    if error_count:
        summary.append(f"{error_count} error(s)")
    if warning_count:
        summary.append(f"{warning_count} warning(s)")
    if info_count:
        summary.append(f"{info_count} info")

        header = f"Found {', '.join(summary)}:\n\n" if summary else ""
    return header + "\n".join(lines)


# Note: Removed hardcoded _run_pyright() and _run_typescript_diagnostics() functions.
# The plugin now uses the generic LSP protocol via _get_lsp_diagnostics() which
# works with any LSP-compliant server (pyright, typescript-language-server, gopls, etc.)


def _parse_position(target: str) -> tuple[Path, int, int]:
    """Parse a position string like 'file.py:10:5' into (path, line, col).

    Args:
        target: Position string in format 'file:line:col' or 'file:line'

    Returns:
        Tuple of (Path, line, column) - line and column are 1-indexed

    Raises:
        ValueError: If target format is invalid

    Note:
        Handles Windows paths with drive letters (e.g., C:/path/file.py:10:5)
    """
    # Handle Windows drive letters (e.g., C:/path/file.py:10:5)
    # We need to be careful not to split on the drive letter colon
    if len(target) >= 2 and target[1] == ":" and target[0].isalpha():
        # Windows path - split only from after the drive letter
        rest = target[2:]
        parts = rest.rsplit(":", 2)
        # Reconstruct file path with drive letter
        if len(parts) >= 1:
            parts[0] = target[0:2] + parts[0]
    else:
        parts = target.rsplit(":", 2)

    if len(parts) < 2:
        raise ValueError(
            f"Invalid position format: {target}. Expected 'file:line' or 'file:line:col'"
        )

    if len(parts) == 2:
        file_path, line_str = parts
        col = 1
    else:
        file_path, line_str, col_str = parts
        try:
            col = int(col_str)
        except ValueError:
            raise ValueError(f"Invalid column number: {col_str}")

    try:
        line = int(line_str)
    except ValueError:
        raise ValueError(f"Invalid line number: {line_str}")

    return Path(file_path), line, col


def _get_lsp_definition(
    file: Path, line: int, column: int, workspace: Path | None = None
) -> list[Location] | None:
    """Get definition location(s) for a symbol using LSP.

    Returns list of Location objects, or None if LSP not available.
    """
    suffix = file.suffix.lower()
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if not language:
        return None

    if workspace is None:
        workspace = _get_workspace() or file.parent

    server = _get_or_start_server(language, workspace)
    if server is None:
        return None

    return server.get_definition(file, line, column)


def _get_lsp_references(
    file: Path, line: int, column: int, workspace: Path | None = None
) -> list[Location] | None:
    """Find all references to a symbol using LSP.

    Returns list of Location objects, or None if LSP not available.
    """
    suffix = file.suffix.lower()
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if not language:
        return None

    if workspace is None:
        workspace = _get_workspace() or file.parent

    server = _get_or_start_server(language, workspace)
    if server is None:
        return None

    return server.get_references(file, line, column)


def _get_lsp_hover(
    file: Path, line: int, column: int, workspace: Path | None = None
) -> HoverInfo | None:
    """Get hover information for a symbol using LSP.

    Returns HoverInfo object, or None if LSP not available or no info.
    """
    suffix = file.suffix.lower()
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if not language:
        return None

    if workspace is None:
        workspace = _get_workspace() or file.parent

    server = _get_or_start_server(language, workspace)
    if server is None:
        return None

    return server.get_hover(file, line, column)


def _get_lsp_rename(
    file: Path, line: int, column: int, new_name: str, workspace: Path | None = None
) -> WorkspaceEdit | None:
    """Rename a symbol using LSP.

    Returns WorkspaceEdit containing all changes, or None if LSP not available.
    Uses prepare_rename to validate the symbol can be renamed first.
    """
    suffix = file.suffix.lower()
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if not language:
        return None

    if workspace is None:
        workspace = _get_workspace() or file.parent

    server = _get_or_start_server(language, workspace)
    if server is None:
        return None

    # Validate rename is possible using prepare_rename (optional server feature)
    # This provides better error messages and catches non-renameable symbols early
    prepare_result = server.prepare_rename(file, line, column)
    if prepare_result is None:
        logger.debug(
            "prepare_rename returned None - symbol may not be renameable or "
            "server doesn't support prepareRename"
        )
        # Continue anyway - not all servers support prepareRename

    return server.rename(file, line, column, new_name)


def _get_lsp_format(
    file: Path,
    workspace: Path | None = None,
    tab_size: int = 4,
    insert_spaces: bool = True,
) -> list[TextEdit] | None:
    """Format a document using LSP.

    Returns list of TextEdit objects to apply, or None if LSP not available.
    """
    suffix = file.suffix.lower()
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if not language:
        return None

    if workspace is None:
        workspace = _get_workspace() or file.parent

    server = _get_or_start_server(language, workspace)
    if server is None:
        return None

    return server.format_document(file, tab_size, insert_spaces)


def _get_lsp_signature(
    file: Path, line: int, column: int, workspace: Path | None = None
) -> SignatureInfo | None:
    """Get signature help for a function call using LSP.

    Returns SignatureInfo object or None if LSP not available.
    """
    suffix = file.suffix.lower()
    language = EXTENSION_TO_LANGUAGE.get(suffix)
    if not language:
        return None

    if workspace is None:
        workspace = _get_workspace() or file.parent

    server = _get_or_start_server(language, workspace)
    if server is None:
        return None

    return server.get_signature_help(file, line, column)


def execute(
    code: str | None,
    args: list[str] | None,
    kwargs: dict[str, str] | None,
    confirm: "ConfirmFunc",
) -> Message:
    """Execute the LSP tool.

    Usage:
        lsp diagnostics <file>     - Get errors/warnings for a file
        lsp definition <file:line:col> - Jump to definition
        lsp references <file:line:col> - Find all references
        lsp hover <file:line:col>      - Get documentation/type info
        lsp rename <file:line:col> <new_name> - Rename symbol across project
        lsp status                 - Show available language servers
        lsp check                  - Run diagnostics on all changed files
    """
    if args is None or not args:
        return Message(
            "system",
            "Usage: lsp <action> [args]\n\n"
            "Actions:\n"
            "  diagnostics <file>       - Get errors/warnings for a file\n"
            "  definition <file:line:col> - Jump to symbol definition\n"
            "  references <file:line:col> - Find all references to symbol\n"
            "  hover <file:line:col>      - Get documentation/type info\n"
            "  rename <file:line:col> <new_name> - Rename symbol across project\n"
            "  format <file>            - Format document using LSP\n"
            "  signature <file:line:col> - Get function signature help\n"
            "  status                   - Show available language servers\n"
            "  check                    - Run diagnostics on all changed files (git)",
        )

    action = args[0].lower()
    workspace = _get_workspace()

    if action == "diagnostics":
        if len(args) < 2:
            return Message("system", "Usage: lsp diagnostics <file>")

        file_path = args[1]
        file = Path(file_path)

        # Make absolute if relative
        if not file.is_absolute() and workspace:
            file = workspace / file

        if not file.exists():
            return Message("system", f"Error: File not found: {file}")

        # Determine file type and run appropriate checker
        suffix = file.suffix.lower()

        supported_extensions = {
            ".py",
            ".pyi",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".go",
            ".rs",
        }
        if suffix not in supported_extensions:
            return Message(
                "system",
                f"Unsupported file type: {suffix}\n\n"
                "Supported: .py, .pyi (Python), .ts, .tsx, .js, .jsx (TypeScript/JavaScript), "
                ".go (Go), .rs (Rust)",
            )

        # Use proper LSP protocol (generic, works with any language server)
        result = _get_lsp_diagnostics(file, workspace)

        # If LSP server isn't available, provide helpful installation instructions
        if result is None:
            install_hints = {
                ".py": "pyright (npm install -g pyright) or pylsp (pip install python-lsp-server)",
                ".pyi": "pyright (npm install -g pyright) or pylsp (pip install python-lsp-server)",
                ".ts": "typescript-language-server (npm install -g typescript-language-server typescript)",
                ".tsx": "typescript-language-server (npm install -g typescript-language-server typescript)",
                ".js": "typescript-language-server (npm install -g typescript-language-server typescript)",
                ".jsx": "typescript-language-server (npm install -g typescript-language-server typescript)",
                ".go": "gopls (go install golang.org/x/tools/gopls@latest)",
                ".rs": "rust-analyzer (rustup component add rust-analyzer)",
            }
            hint = install_hints.get(
                suffix, "the appropriate LSP server for this language"
            )
            return Message(
                "system",
                f"No LSP server available for {suffix} files.\n\nInstall: {hint}",
            )

        return Message("system", f"**Diagnostics for {file.name}**\n\n{result}")

    elif action == "definition":
        if len(args) < 2:
            return Message("system", "Usage: lsp definition <file:line:col>")

        try:
            file, line, col = _parse_position(args[1])
        except ValueError as e:
            return Message("system", f"Error: {e}")

        # Make absolute if relative
        if not file.is_absolute() and workspace:
            file = workspace / file

        if not file.exists():
            return Message("system", f"Error: File not found: {file}")

        locations = _get_lsp_definition(file, line, col, workspace)

        if locations is None:
            suffix = file.suffix.lower()
            return Message(
                "system",
                f"No LSP server available for {suffix} files.\n\n"
                "Install the appropriate language server to enable this feature.",
            )

        if not locations:
            return Message(
                "system", f"No definition found for symbol at {file.name}:{line}:{col}"
            )

        # Format results
        lines = [f"**Definition for symbol at {file.name}:{line}:{col}**\n"]
        for loc in locations:
            lines.append(f"📍 {loc}")
        return Message("system", "\n".join(lines))

    elif action == "references":
        if len(args) < 2:
            return Message("system", "Usage: lsp references <file:line:col>")

        try:
            file, line, col = _parse_position(args[1])
        except ValueError as e:
            return Message("system", f"Error: {e}")

        # Make absolute if relative
        if not file.is_absolute() and workspace:
            file = workspace / file

        if not file.exists():
            return Message("system", f"Error: File not found: {file}")

        locations = _get_lsp_references(file, line, col, workspace)

        if locations is None:
            suffix = file.suffix.lower()
            return Message(
                "system",
                f"No LSP server available for {suffix} files.\n\n"
                "Install the appropriate language server to enable this feature.",
            )

        if not locations:
            return Message(
                "system", f"No references found for symbol at {file.name}:{line}:{col}"
            )

        # Format results
        lines = [
            f"**References for symbol at {file.name}:{line}:{col}** ({len(locations)} found)\n"
        ]
        for loc in locations:
            lines.append(f"📍 {loc}")
        return Message("system", "\n".join(lines))

    elif action == "hover":
        if len(args) < 2:
            return Message("system", "Usage: lsp hover <file:line:col>")

        try:
            file, line, col = _parse_position(args[1])
        except ValueError as e:
            return Message("system", f"Error: {e}")

        # Make absolute if relative
        if not file.is_absolute() and workspace:
            file = workspace / file

        if not file.exists():
            return Message("system", f"Error: File not found: {file}")

        hover_info = _get_lsp_hover(file, line, col, workspace)

        if hover_info is None:
            suffix = file.suffix.lower()
            return Message(
                "system",
                f"No hover information available for symbol at {file.name}:{line}:{col}\n\n"
                "This could mean:\n"
                "- No LSP server is available\n"
                "- The position doesn't point to a symbol\n"
                "- The language server doesn't have info for this symbol",
            )

        return Message(
            "system",
            f"**Hover info at {file.name}:{line}:{col}**\n\n{hover_info.contents}",
        )

    elif action == "rename":
        if len(args) < 3:
            return Message("system", "Usage: lsp rename <file:line:col> <new_name>")

        try:
            file, line, col = _parse_position(args[1])
        except ValueError as e:
            return Message("system", f"Error: {e}")

        new_name = args[2]

        # Make absolute if relative
        if not file.is_absolute() and workspace:
            file = workspace / file

        if not file.exists():
            return Message("system", f"Error: File not found: {file}")

        workspace_edit = _get_lsp_rename(file, line, col, new_name, workspace)

        if workspace_edit is None:
            return Message(
                "system",
                f"Cannot rename symbol at {file.name}:{line}:{col}\n\n"
                "This could mean:\n"
                "- No LSP server is available\n"
                "- The position doesn't point to a renameable symbol\n"
                "- The language server doesn't support rename",
            )

        # Format the workspace edit for display
        lines = [
            f"**Rename to '{new_name}'** - {workspace_edit.edit_count} edit(s) in {workspace_edit.file_count} file(s)\n"
        ]

        for file_path, edits in workspace_edit.edits_by_file.items():
            rel_path = Path(file_path)
            if workspace and rel_path.is_absolute():
                try:
                    rel_path = rel_path.relative_to(workspace)
                except ValueError:
                    pass

            lines.append(f"\n**{rel_path}**")
            for edit in edits:
                lines.append(
                    f"  - Line {edit.start_line}:{edit.start_column}-{edit.end_line}:{edit.end_column}: "
                    f"`{edit.new_text}`"
                )

        lines.append(
            "\n\n⚠️ **Note:** These changes are shown for preview only. "
            "Use the patch tool to apply the edits to each file."
        )

        return Message("system", "\n".join(lines))

    elif action == "format":
        if len(args) < 2:
            return Message("system", "Usage: lsp format <file>")

        file_path = args[1]
        file = Path(file_path)

        # Make absolute if relative
        if not file.is_absolute() and workspace:
            file = workspace / file

        if not file.exists():
            return Message("system", f"Error: File not found: {file}")

        # Parse optional formatting options
        tab_size = 4
        insert_spaces = True
        if len(args) >= 3:
            try:
                tab_size = int(args[2])
            except ValueError:
                pass
        if len(args) >= 4:
            insert_spaces = args[3].lower() != "tabs"

        edits_result = _get_lsp_format(file, workspace, tab_size, insert_spaces)

        if edits_result is None:
            suffix = file.suffix.lower()
            return Message(
                "system",
                f"No LSP server available for {suffix} files.\n\n"
                "Install the appropriate language server to enable formatting.",
            )

        edits = edits_result  # Type narrowing: edits is now list[TextEdit]

        if not edits:
            return Message("system", f"✅ {file.name} is already properly formatted.")

        # Format and show the edits
        lines = [f"**Format {file.name}** - {len(edits)} edit(s)\n"]

        # Group edits by line for cleaner display
        for edit in edits[:20]:  # Limit display to 20 edits
            if edit.start_line == edit.end_line:
                preview = (
                    edit.new_text[:50] + "..."
                    if len(edit.new_text) > 50
                    else edit.new_text
                )
                preview = preview.replace("\n", "\\n")
                lines.append(f"  - Line {edit.start_line}: `{preview}`")
            else:
                lines.append(
                    f"  - Lines {edit.start_line}-{edit.end_line}: {len(edit.new_text)} chars"
                )

        if len(edits) > 20:
            lines.append(f"\n  ... and {len(edits) - 20} more edit(s)")

        lines.append(
            "\n\n⚠️ **Note:** These changes are shown for preview only. "
            "Use the patch tool to apply the edits."
        )

        return Message("system", "\n".join(lines))

    elif action == "signature":
        if len(args) < 2:
            return Message("system", "Usage: lsp signature <file:line:col>")

        try:
            file, line, col = _parse_position(args[1])
        except ValueError as e:
            return Message("system", f"Error: {e}")

        # Make absolute if relative
        if not file.is_absolute() and workspace:
            file = workspace / file

        if not file.exists():
            return Message("system", f"Error: File not found: {file}")

        sig_info = _get_lsp_signature(file, line, col, workspace)

        if sig_info is None or not sig_info.signatures:
            return Message(
                "system",
                f"No signature help available at {file.name}:{line}:{col}\n\n"
                "This could mean:\n"
                "- No LSP server is available\n"
                "- The position is not inside a function call\n"
                "- The language server doesn't have signature info",
            )

        # Format signature help
        lines = [f"**Signature help at {file.name}:{line}:{col}**\n"]

        for i, sig in enumerate(sig_info.signatures):
            prefix = "→ " if i == sig_info.active_signature else "  "
            lines.append(f"{prefix}`{sig.label}`")

            if sig.documentation:
                # Truncate long documentation
                doc = (
                    sig.documentation[:200] + "..."
                    if len(sig.documentation) > 200
                    else sig.documentation
                )
                lines.append(f"\n{doc}\n")

            # Show parameters if available
            if sig.parameters:
                lines.append("\n**Parameters:**")
                for j, param in enumerate(sig.parameters):
                    active = "→ " if sig_info.active_parameter == j else "  "
                    param_line = f"{active}{param.label}"
                    if param.documentation:
                        param_doc = (
                            param.documentation[:100] + "..."
                            if len(param.documentation) > 100
                            else param.documentation
                        )
                        param_line += f" - {param_doc}"
                    lines.append(param_line)

        return Message("system", "\n".join(lines))

    elif action == "status":
        status_lines = ["**LSP Status**\n"]

        # Check available language servers
        servers = [
            ("pyright", "pyright --version", "Python"),
            (
                "typescript-language-server",
                "typescript-language-server --version",
                "TypeScript/JavaScript",
            ),
            ("gopls", "gopls version", "Go"),
            ("rust-analyzer", "rust-analyzer --version", "Rust"),
        ]

        for name, check_cmd, lang in servers:
            try:
                check_result = subprocess.run(
                    check_cmd.split(),
                    capture_output=True,
                    timeout=5,
                )
                if check_result.returncode == 0:
                    status_lines.append(f"✅ {name} ({lang})")
                else:
                    status_lines.append(f"❌ {name} ({lang}) - not found")
            except Exception:
                status_lines.append(f"❌ {name} ({lang}) - not found")

        status_lines.append(
            "\n**Workspace:** " + (str(workspace) if workspace else "Unknown")
        )

        return Message("system", "\n".join(status_lines))

    elif action == "check":
        # Run diagnostics on all changed files (supports any language with LSP)
        if workspace is None:
            return Message("system", "Error: Could not determine workspace")

        try:
            # Get changed files from git
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True,
                text=True,
                cwd=workspace,
                timeout=10,
            )
            changed_files = (
                diff_result.stdout.strip().split("\n")
                if diff_result.stdout.strip()
                else []
            )

            # Also check staged files
            staged_result = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True,
                text=True,
                cwd=workspace,
                timeout=10,
            )
            staged_files = (
                staged_result.stdout.strip().split("\n")
                if staged_result.stdout.strip()
                else []
            )

            all_files = set(changed_files + staged_files)

            # Filter to supported file types
            supported_extensions = {
                ".py",
                ".pyi",  # Python
                ".ts",
                ".tsx",
                ".js",
                ".jsx",  # TypeScript/JavaScript
                ".go",  # Go
                ".rs",  # Rust
            }
            lsp_files = [
                f
                for f in all_files
                if Path(f).suffix.lower() in supported_extensions
                and (workspace / f).exists()
            ]

            if not lsp_files:
                return Message("system", "No changed files with LSP support to check.")

            all_results = []
            files_checked = 0
            for lsp_file in lsp_files[:10]:  # Limit to 10 files
                full_path = workspace / lsp_file
                diag_result = _get_lsp_diagnostics(full_path, workspace)
                if diag_result is None:
                    # No LSP server available for this file type, skip
                    continue
                files_checked += 1
                if (
                    "No errors" not in diag_result
                    and "No diagnostics" not in diag_result
                    and "✅" not in diag_result
                ):
                    all_results.append(f"**{lsp_file}**\n{diag_result}")

            if files_checked == 0:
                return Message(
                    "system",
                    f"No LSP servers available for the {len(lsp_files)} changed file(s).\n\n"
                    "Install appropriate LSP servers (pyright, typescript-language-server, gopls, rust-analyzer).",
                )

            if not all_results:
                return Message(
                    "system",
                    f"✅ All {files_checked} checked file(s) pass diagnostics.",
                )

            return Message(
                "system",
                f"**Diagnostics for {files_checked} changed file(s):**\n\n"
                + "\n\n---\n\n".join(all_results),
            )

        except Exception as e:
            return Message("system", f"Error checking files: {e}")

    else:
        return Message(
            "system",
            f"Unknown action: {action}\n\n"
            "Available actions: diagnostics, definition, references, hover, rename, status, check",
        )


def _lsp_command(ctx: "CommandContext") -> Generator[Message, None, None]:
    """Handler for /lsp command.

    Usage:
        /lsp                      - Show status of available LSP servers
        /lsp diagnostics <file>   - Get errors/warnings for a file
        /lsp status               - Show available language servers
        /lsp check                - Run diagnostics on all changed files
    """
    # Parse arguments
    args = ctx.args if ctx.args else []

    # Default to status if no args
    if not args:
        args = ["status"]

    # Use the execute function with the parsed args
    result = execute(
        code=None,
        args=args,
        kwargs=None,
        confirm=ctx.confirm,
    )
    yield result


# Tool specification
tool = ToolSpec(
    name="lsp",
    desc="Language Server Protocol integration for code intelligence",
    instructions="""LSP helps you understand and modify code with IDE-level intelligence.

**When to use LSP:**
- Before making changes: Run `lsp diagnostics <file>` to find existing errors
- Understanding unfamiliar code: Use `lsp hover <file:line:col>` for types/docs, `lsp definition` to find implementations
- Safe refactoring: Use `lsp rename <file:line:col> <new_name>` for project-wide symbol renames
- Code cleanup: Use `lsp format <file>` to apply consistent formatting
- Writing function calls: Use `lsp signature <file:line:col>` to see parameter info

**Commands:**
- `lsp diagnostics <file>` - Find errors/warnings before and after changes
- `lsp check` - Check all modified files (git-aware)
- `lsp definition <file:line:col>` - Jump to where a symbol is defined
- `lsp references <file:line:col>` - Find all usages of a symbol
- `lsp hover <file:line:col>` - Get type info and documentation
- `lsp rename <file:line:col> <new_name>` - Rename symbol across entire project
- `lsp format <file>` - Auto-format document (preview only)
- `lsp signature <file:line:col>` - Get function signature and parameter docs
- `lsp status` - Check which language servers are available

**Supported:** Python (pyright), TypeScript/JS (ts-server), Go (gopls), Rust (rust-analyzer)
""",
    execute=execute,
    block_types=["lsp"],
    parameters=[
        Parameter(
            name="action",
            type="string",
            description="Action: diagnostics, definition, references, hover, rename, format, signature, status, check",
            required=True,
        ),
        Parameter(
            name="target",
            type="string",
            description="File path or position (file:line:col) depending on action",
            required=False,
        ),
    ],
    commands={"lsp": _lsp_command},
)


# Note: The tool is automatically discovered by gptme's plugin system
# when loading from the tools/ directory. No explicit registration needed.
