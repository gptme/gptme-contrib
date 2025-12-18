"""LSP tool for gptme - provides code intelligence via Language Server Protocol.

This tool provides access to LSP features:
- diagnostics: Get errors/warnings for a file
- definition: Jump to definition (Phase 2)
- references: Find all references (Phase 2)
- hover: Get documentation (Phase 2)

Uses proper LSP protocol to communicate with language servers generically,
supporting pyright (Python), typescript-language-server (JS/TS), gopls (Go),
rust-analyzer (Rust), and any other LSP-compliant server.
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.message import Message
from gptme.tools.base import ConfirmFunc, Parameter, ToolSpec

from ..lsp_client import LSPServer, KNOWN_SERVERS

if TYPE_CHECKING:
    from gptme.tools.base import ConfirmFunc  # noqa: F811

logger = logging.getLogger(__name__)

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
    ext_to_lang = {
        ".py": "python",
        ".pyi": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
    }

    suffix = file.suffix.lower()
    language = ext_to_lang.get(suffix)
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


def execute(
    code: str | None,
    args: list[str] | None,
    kwargs: dict[str, str] | None,
    confirm: "ConfirmFunc",
) -> Message:
    """Execute the LSP tool.

    Usage:
        lsp diagnostics <file>     - Get errors/warnings for a file
        lsp status                 - Show available language servers
        lsp check                  - Run diagnostics on all changed files
    """
    if args is None or not args:
        return Message(
            "system",
            "Usage: lsp <action> [args]\n\n"
            "Actions:\n"
            "  diagnostics <file>  - Get errors/warnings for a file\n"
            "  status              - Show available language servers\n"
            "  check               - Run diagnostics on all changed files (git)",
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
                f"No LSP server available for {suffix} files.\n\n" f"Install: {hint}",
            )

        return Message("system", f"**Diagnostics for {file.name}**\n\n{result}")

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
            "Available actions: diagnostics, status, check",
        )


# Tool specification
tool = ToolSpec(
    name="lsp",
    desc="Language Server Protocol integration for code intelligence",
    instructions="""Use LSP to get code intelligence:

- `lsp diagnostics <file>` - Get errors/warnings for a Python or TypeScript file
- `lsp status` - Show available language servers
- `lsp check` - Run diagnostics on all changed files (git)

This tool helps catch errors before running code, find issues in edited files,
and maintain code quality.

**Supported languages:**
- Python (requires pyright: `npm install -g pyright`)
- TypeScript/JavaScript (requires tsc via npm)

**Example usage:**
```lsp
diagnostics src/myfile.py
```
""",
    execute=execute,
    block_types=["lsp"],
    parameters=[
        Parameter(
            name="action",
            type="string",
            description="Action to perform: diagnostics, status, or check",
            required=True,
        ),
        Parameter(
            name="file",
            type="string",
            description="File path for diagnostics action",
            required=False,
        ),
    ],
)


# Note: The tool is automatically discovered by gptme's plugin system
# when loading from the tools/ directory. No explicit registration needed.
