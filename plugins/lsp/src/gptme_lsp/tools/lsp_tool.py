"""LSP tool for gptme - provides code intelligence via Language Server Protocol.

This tool provides access to LSP features:
- diagnostics: Get errors/warnings for a file
- definition: Jump to definition (Phase 2)
- references: Find all references (Phase 2)
- hover: Get documentation (Phase 2)

Phase 1 focuses on diagnostics - the most reliable and actionable feature.
"""

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from gptme.message import Message
from gptme.tools.base import ConfirmFunc, Parameter, ToolSpec

if TYPE_CHECKING:
    from gptme.tools.base import ConfirmFunc  # noqa: F811

logger = logging.getLogger(__name__)

# Global manager instance (initialized per workspace)
_manager = None


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


def _ensure_pyright() -> bool:
    """Check if pyright is available."""
    try:
        result = subprocess.run(
            ["pyright", "--version"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run_pyright(file: Path, workspace: Path | None = None) -> str:
    """Run pyright on a file and return diagnostics as formatted string.

    This is a simpler approach than full LSP client - just call pyright CLI.
    """
    try:
        cmd = ["pyright", "--outputjson", str(file)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=60,
        )

        # Parse JSON output
        import json

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            # Pyright might output non-JSON on error
            if result.returncode != 0:
                return f"Error running pyright: {result.stderr or result.stdout}"
            return "No diagnostics found."

        diagnostics = data.get("generalDiagnostics", [])
        if not diagnostics:
            return "✅ No errors or warnings found."

        # Format diagnostics
        lines = []
        error_count = 0
        warning_count = 0
        info_count = 0

        for diag in diagnostics:
            severity = diag.get("severity", "error")
            file_path = diag.get("file", "unknown")
            range_info = diag.get("range", {})
            start = range_info.get("start", {})
            line = start.get("line", 0) + 1  # 0-indexed to 1-indexed
            col = start.get("character", 0) + 1
            message = diag.get("message", "Unknown error")
            rule = diag.get("rule", "")

            # Count by severity
            if severity == "error":
                error_count += 1
                sev_emoji = "❌"
            elif severity == "warning":
                warning_count += 1
                sev_emoji = "⚠️"
            else:
                info_count += 1
                sev_emoji = "ℹ️"

            # Format location relative to workspace
            rel_path = file_path
            if workspace:
                try:
                    rel_path = Path(file_path).relative_to(workspace)
                except ValueError:
                    pass

            rule_str = f" [{rule}]" if rule else ""
            lines.append(f"{sev_emoji} {rel_path}:{line}:{col}: {message}{rule_str}")

        # Summary
        summary_parts = []
        if error_count:
            summary_parts.append(f"{error_count} error(s)")
        if warning_count:
            summary_parts.append(f"{warning_count} warning(s)")
        if info_count:
            summary_parts.append(f"{info_count} info")

        summary = f"Found {', '.join(summary_parts)}" if summary_parts else "No issues"

        return f"{summary}:\n\n" + "\n".join(lines)

    except subprocess.TimeoutExpired:
        return "Error: pyright timed out (>60s)"
    except Exception as e:
        return f"Error running pyright: {e}"


def _run_typescript_diagnostics(file: Path, workspace: Path | None = None) -> str:
    """Run TypeScript compiler to get diagnostics."""
    try:
        # Use tsc for diagnostics
        cmd = ["npx", "tsc", "--noEmit", "--pretty", "false", str(file)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=workspace,
            timeout=60,
        )

        if result.returncode == 0:
            return "✅ No TypeScript errors found."

        # Parse tsc output
        output = result.stdout + result.stderr
        if not output.strip():
            return "✅ No TypeScript errors found."

        lines = output.strip().split("\n")
        error_count = len([line for line in lines if ": error TS" in line])
        warning_count = len([line for line in lines if ": warning TS" in line])

        summary_parts = []
        if error_count:
            summary_parts.append(f"{error_count} error(s)")
        if warning_count:
            summary_parts.append(f"{warning_count} warning(s)")

        summary = (
            f"Found {', '.join(summary_parts)}" if summary_parts else "Issues found"
        )

        # Limit output
        if len(lines) > 20:
            lines = lines[:20] + [f"... and {len(lines) - 20} more"]

        return f"{summary}:\n\n" + "\n".join(lines)

    except subprocess.TimeoutExpired:
        return "Error: TypeScript check timed out (>60s)"
    except Exception as e:
        return f"Error running TypeScript check: {e}"


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

        if suffix in (".py", ".pyi"):
            if not _ensure_pyright():
                return Message(
                    "system",
                    "Error: pyright not found. Install with: npm install -g pyright",
                )
            result = _run_pyright(file, workspace)
        elif suffix in (".ts", ".tsx", ".js", ".jsx"):
            result = _run_typescript_diagnostics(file, workspace)
        else:
            return Message(
                "system",
                f"Unsupported file type: {suffix}\n\n"
                "Supported: .py, .pyi (Python), .ts, .tsx, .js, .jsx (TypeScript/JavaScript)",
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
        # Run diagnostics on all changed Python files
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
            py_files = [
                f
                for f in all_files
                if f.endswith((".py", ".pyi")) and (workspace / f).exists()
            ]

            if not py_files:
                return Message("system", "No changed Python files to check.")

            # Run pyright on all files
            if not _ensure_pyright():
                return Message(
                    "system",
                    "Error: pyright not found. Install with: npm install -g pyright",
                )

            all_results = []
            for py_file in py_files[:10]:  # Limit to 10 files
                full_path = Path(workspace) / py_file
                diag_result = _run_pyright(full_path, workspace)
                if (
                    "No errors" not in diag_result
                    and "No diagnostics" not in diag_result
                ):
                    all_results.append(f"**{py_file}**\n{diag_result}")

            if not all_results:
                return Message(
                    "system",
                    f"✅ All {len(py_files)} changed Python file(s) pass diagnostics.",
                )

            return Message(
                "system",
                f"**Diagnostics for {len(py_files)} changed file(s):**\n\n"
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
