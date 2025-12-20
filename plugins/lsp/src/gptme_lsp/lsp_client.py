"""LSP client implementation for communicating with language servers.

This module provides a simple LSP client that can:
1. Start and manage language server processes
2. Send JSON-RPC requests
3. Parse LSP responses

Initially focused on diagnostics (Phase 1).
"""

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import unquote, urlparse

from .config import format_server_error, load_config

logger = logging.getLogger(__name__)


@dataclass
class Diagnostic:
    """Represents an LSP diagnostic (error/warning/info)."""

    file: Path
    line: int  # 1-indexed for display
    column: int  # 1-indexed for display
    severity: str  # error, warning, info, hint
    message: str
    source: str | None = None  # e.g., "pyright", "typescript"
    code: str | None = None  # e.g., "reportGeneralTypeIssues"

    def __str__(self) -> str:
        sev = self.severity.upper()[:3]
        location = f"{self.file}:{self.line}:{self.column}"
        return f"[{sev}] {location}: {self.message}"


@dataclass
class Location:
    """Represents an LSP location (file + position)."""

    file: Path
    line: int  # 1-indexed for display
    column: int  # 1-indexed for display
    end_line: int | None = None
    end_column: int | None = None

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.column}"


@dataclass
class HoverInfo:
    """Represents hover information from LSP."""

    contents: str  # Markdown or plain text
    range_start: tuple[int, int] | None = None  # (line, column), 1-indexed
    range_end: tuple[int, int] | None = None

    def __str__(self) -> str:
        return self.contents


@dataclass
class TextEdit:
    """Represents a single text edit (insert, replace, or delete)."""

    file: Path
    start_line: int  # 1-indexed
    start_column: int  # 1-indexed
    end_line: int  # 1-indexed
    end_column: int  # 1-indexed
    new_text: str

    def __str__(self) -> str:
        return f"{self.file}:{self.start_line}:{self.start_column}-{self.end_line}:{self.end_column}"


@dataclass
class WorkspaceEdit:
    """Represents a set of changes across multiple files (e.g., from rename)."""

    edits: list[TextEdit]
    # Maps file path to list of edits for that file
    edits_by_file: dict[str, list[TextEdit]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Group edits by file for easier processing."""
        for edit in self.edits:
            file_str = str(edit.file)
            if file_str not in self.edits_by_file:
                self.edits_by_file[file_str] = []
            self.edits_by_file[file_str].append(edit)

    @property
    def file_count(self) -> int:
        """Number of files affected."""
        return len(self.edits_by_file)

    @property
    def edit_count(self) -> int:
        """Total number of edits."""
        return len(self.edits)

    def __str__(self) -> str:
        return f"WorkspaceEdit({self.edit_count} edits in {self.file_count} files)"


@dataclass
class SignatureParameter:
    """Represents a parameter in a function signature."""

    label: str
    documentation: str | None = None


@dataclass
class SignatureInfo:
    """Represents signature help information from LSP."""

    signatures: list["SignatureLabel"]  # List of overloads
    active_signature: int = 0
    active_parameter: int | None = None

    def __str__(self) -> str:
        if not self.signatures:
            return "No signature information"
        sig = (
            self.signatures[self.active_signature]
            if self.active_signature < len(self.signatures)
            else self.signatures[0]
        )
        return sig.label


@dataclass
class SignatureLabel:
    """Represents a single function signature."""

    label: str  # Full signature string e.g., "def foo(x: int, y: str) -> bool"
    documentation: str | None = None
    parameters: list[SignatureParameter] = field(default_factory=list)


@dataclass
class InlayHint:
    """Represents an inlay hint from LSP (Phase 5).

    Inlay hints are inline annotations showing parameter names, types, etc.
    """

    line: int
    column: int
    label: str
    kind: str | None = None  # "type", "parameter", or None
    padding_left: bool = False
    padding_right: bool = False

    def __str__(self) -> str:
        kind_str = f" ({self.kind})" if self.kind else ""
        return f"{self.line}:{self.column}: {self.label}{kind_str}"


@dataclass
class CallHierarchyItem:
    """Represents an item in a call hierarchy (Phase 5).

    Used for navigating call relationships in code.
    """

    name: str
    kind: str  # "function", "method", "class", etc.
    file: Path
    line: int
    column: int
    detail: str | None = None
    # Internal: LSP data for follow-up requests
    _data: dict = field(default_factory=dict, repr=False)

    def __str__(self) -> str:
        detail_str = f" - {self.detail}" if self.detail else ""
        return f"{self.name} ({self.kind}){detail_str} at {self.file.name}:{self.line}"


@dataclass
class CallHierarchyCall:
    """Represents an incoming or outgoing call in hierarchy (Phase 5)."""

    item: CallHierarchyItem
    from_ranges: list[tuple[int, int]]  # List of (line, column) where call occurs

    def __str__(self) -> str:
        ranges = ", ".join(f"{line_num}:{col}" for line_num, col in self.from_ranges)
        return f"{self.item.name} at {ranges}"


@dataclass
class CodeAction:
    """Represents an LSP code action (quick fix, refactoring, etc.)."""

    title: str
    kind: str | None = None  # e.g., "quickfix", "refactor", "source.organizeImports"
    edit: WorkspaceEdit | None = None
    diagnostics: list[Diagnostic] | None = None
    is_preferred: bool = False

    def __str__(self) -> str:
        kind_str = f"[{self.kind}]" if self.kind else ""
        return f"{kind_str} {self.title}"


@dataclass
class SymbolInfo:
    """Represents a workspace symbol."""

    name: str
    kind: str  # e.g., "class", "function", "variable", "method"
    location: Location
    container_name: str | None = None  # Parent class/module name

    def __str__(self) -> str:
        container = f" ({self.container_name})" if self.container_name else ""
        return f"[{self.kind}] {self.name}{container} at {self.location}"


@dataclass

# Phase 6 dataclasses


@dataclass
class SemanticToken:
    """Represents a semantic token for rich syntax highlighting (Phase 6)."""

    line: int  # 1-indexed
    column: int  # 1-indexed
    length: int
    token_type: str  # e.g., "function", "variable", "class", "parameter"
    modifiers: list[str]  # e.g., ["declaration", "readonly"]

    def __str__(self) -> str:
        mods = f" [{', '.join(self.modifiers)}]" if self.modifiers else ""
        return (
            f"L{self.line}:{self.column} ({self.length} chars) {self.token_type}{mods}"
        )


@dataclass
class DocumentLink:
    """Represents a clickable link in a document (Phase 6)."""

    start_line: int  # 1-indexed
    start_column: int  # 1-indexed
    end_line: int  # 1-indexed
    end_column: int  # 1-indexed
    target: str | None  # The target URI (may need resolution)
    tooltip: str | None = None

    def __str__(self) -> str:
        loc = f"L{self.start_line}:{self.start_column}-{self.end_column}"
        target_str = f" -> {self.target}" if self.target else " (unresolved)"
        return f"{loc}{target_str}"


@dataclass
class CodeLens:
    """Represents a code lens - actionable annotation above code (Phase 6)."""

    line: int  # 1-indexed
    column: int  # 1-indexed
    command_title: str | None  # e.g., "5 references", "Run test"
    command_id: str | None = None
    command_args: list[Any] | None = None

    def __str__(self) -> str:
        title = self.command_title or "(unresolved)"
        return f"L{self.line}: {title}"

class LSPServer:
    """Manages a language server process."""

    name: str
    command: list[str]
    workspace: Path
    process: subprocess.Popen | None = None
    request_id: int = field(default=0, init=False)
    _reader_thread: Thread | None = field(default=None, init=False)
    _responses: dict[int, Any] = field(default_factory=dict, init=False)
    _initialized: bool = field(default=False, init=False)
    _diagnostics: dict[str, list[Diagnostic]] = field(default_factory=dict, init=False)

    def start(self) -> bool:
        """Start the language server process."""
        if self.process is not None:
            logger.warning(f"Server {self.name} already running")
            return True

        try:
            logger.info(f"Starting {self.name}: {' '.join(self.command)}")
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.workspace,
            )

            # Start reader thread for responses
            self._reader_thread = Thread(target=self._read_responses, daemon=True)
            self._reader_thread.start()

            # Initialize the server
            return self._initialize()
        except FileNotFoundError:
            logger.error(f"Language server not found: {self.command[0]}")
            return False
        except Exception as e:
            logger.error(f"Failed to start {self.name}: {e}")
            return False

    def stop(self) -> None:
        """Stop the language server process."""
        if self.process is not None:
            try:
                self._send_request("shutdown", {})
                self._send_notification("exit", {})
            except Exception:
                pass
            self.process.terminate()
            self.process = None
            self._initialized = False

    def _initialize(self) -> bool:
        """Send initialize request to the server."""
        params = {
            "processId": None,
            "rootUri": self.workspace.as_uri(),
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {"relatedInformation": True},
                }
            },
        }
        result = self._send_request("initialize", params)
        if result is not None:
            self._send_notification("initialized", {})
            self._initialized = True
            logger.info(f"{self.name} initialized successfully")
            return True
        return False

    def _send_request(self, method: str, params: dict) -> Any:
        """Send a JSON-RPC request and wait for response."""
        if self.process is None or self.process.stdin is None:
            return None

        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params,
        }
        self._write_message(request)

        # Wait for response (simple blocking approach for now)
        import time

        timeout = 10  # seconds
        start = time.time()
        while time.time() - start < timeout:
            if self.request_id in self._responses:
                response = self._responses.pop(self.request_id)
                if "error" in response:
                    logger.error(f"LSP error: {response['error']}")
                    return None
                return response.get("result")
            time.sleep(0.1)

        logger.warning(f"Request {method} timed out")
        return None

    def _send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self.process is None or self.process.stdin is None:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._write_message(notification)

    def _write_message(self, message: dict) -> None:
        """Write a JSON-RPC message with Content-Length header."""
        if self.process is None or self.process.stdin is None:
            return

        content = json.dumps(message)
        header = f"Content-Length: {len(content)}\r\n\r\n"
        try:
            self.process.stdin.write(header.encode() + content.encode())
            self.process.stdin.flush()
        except Exception as e:
            logger.error(f"Failed to write message: {e}")

    def _read_responses(self) -> None:
        """Read JSON-RPC responses from the server (runs in background thread)."""
        if self.process is None or self.process.stdout is None:
            return

        try:
            while self.process.poll() is None:
                # Read Content-Length header
                header = b""
                while b"\r\n\r\n" not in header:
                    chunk = self.process.stdout.read(1)
                    if not chunk:
                        return
                    header += chunk

                # Parse Content-Length
                header_str = header.decode()
                content_length = 0
                for line in header_str.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":")[1].strip())
                        break

                if content_length == 0:
                    continue

                # Read content
                content = self.process.stdout.read(content_length)
                if not content:
                    return

                try:
                    message = json.loads(content.decode())
                    if "id" in message:
                        self._responses[message["id"]] = message
                    elif "method" in message:
                        # Handle notifications (e.g., publishDiagnostics)
                        self._handle_notification(message)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse LSP message: {e}")
        except Exception as e:
            logger.error(f"Reader thread error: {e}")

    def _handle_notification(self, message: dict) -> None:
        """Handle notifications from the server."""
        method = message.get("method", "")
        if method == "textDocument/publishDiagnostics":
            # Store diagnostics for later retrieval
            params = message.get("params", {})
            uri = params.get("uri", "")
            raw_diagnostics = params.get("diagnostics", [])
            logger.debug(f"Received {len(raw_diagnostics)} diagnostics for {uri}")

            # Parse URI to file path

            parsed = urlparse(uri)
            file_path = Path(unquote(parsed.path))

            # Convert to Diagnostic objects
            diagnostics: list[Diagnostic] = []
            for diag in raw_diagnostics:
                severity_map = {1: "error", 2: "warning", 3: "info", 4: "hint"}
                severity = severity_map.get(diag.get("severity", 1), "error")
                range_info = diag.get("range", {})
                start = range_info.get("start", {})
                diagnostics.append(
                    Diagnostic(
                        file=file_path,
                        line=start.get("line", 0) + 1,  # 0-indexed to 1-indexed
                        column=start.get("character", 0) + 1,
                        severity=severity,
                        message=diag.get("message", ""),
                        source=diag.get("source"),
                        code=str(diag.get("code")) if diag.get("code") else None,
                    )
                )

            # Store by URI for retrieval
            self._diagnostics[uri] = diagnostics

    def get_diagnostics(self, file: Path) -> list[Diagnostic]:
        """Get diagnostics for a file by opening and requesting them.

        Note: Most language servers push diagnostics via publishDiagnostics
        notification after opening a file.
        """
        if not self._initialized or self.process is None:
            return []

        # Open the file (triggers diagnostics) - reuse _ensure_file_open
        try:
            uri = self._ensure_file_open(file)
        except Exception as e:
            logger.error(f"Cannot open file {file}: {e}")
            return []

        # Wait for diagnostics to arrive (server sends them asynchronously)
        import time

        # Poll for diagnostics with timeout
        timeout = 5.0  # seconds
        poll_interval = 0.1
        elapsed = 0.0
        while elapsed < timeout:
            if uri in self._diagnostics:
                return self._diagnostics[uri]
            time.sleep(poll_interval)
            elapsed += poll_interval

        # Return empty if no diagnostics received (file might have no issues)
        logger.debug(f"No diagnostics received for {uri} after {timeout}s")
        return self._diagnostics.get(uri, [])

    def _ensure_file_open(self, file: Path) -> str:
        """Ensure a file is open in the language server. Returns URI."""
        uri = file.as_uri()
        try:
            content = file.read_text()
        except Exception as e:
            logger.error(f"Cannot read file {file}: {e}")
            raise

        self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self._guess_language_id(file),
                    "version": 1,
                    "text": content,
                }
            },
        )
        return uri

    def get_definition(self, file: Path, line: int, column: int) -> list[Location]:
        """Get definition location(s) for a symbol.

        Args:
            file: The file path
            line: 1-indexed line number
            column: 1-indexed column number

        Returns:
            List of Location objects representing definition locations.
        """
        if not self._initialized or self.process is None:
            return []

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return []

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": column - 1},
            },
        )

        return self._parse_locations(result)

    def get_references(
        self, file: Path, line: int, column: int, include_declaration: bool = True
    ) -> list[Location]:
        """Find all references to a symbol.

        Args:
            file: The file path
            line: 1-indexed line number
            column: 1-indexed column number
            include_declaration: Whether to include the declaration in results

        Returns:
            List of Location objects representing reference locations.
        """
        if not self._initialized or self.process is None:
            return []

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return []

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": column - 1},
                "context": {"includeDeclaration": include_declaration},
            },
        )

        return self._parse_locations(result)

    def get_hover(self, file: Path, line: int, column: int) -> HoverInfo | None:
        """Get hover information (documentation, type info) for a symbol.

        Args:
            file: The file path
            line: 1-indexed line number
            column: 1-indexed column number

        Returns:
            HoverInfo object or None if no hover info available.
        """
        if not self._initialized or self.process is None:
            return None

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return None

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": column - 1},
            },
        )

        if result is None:
            return None

        return self._parse_hover(result)

    def rename(
        self, file: Path, line: int, column: int, new_name: str
    ) -> WorkspaceEdit | None:
        """Rename a symbol across the project.

        Args:
            file: The file path containing the symbol
            line: 1-indexed line number
            column: 1-indexed column number
            new_name: The new name for the symbol

        Returns:
            WorkspaceEdit containing all changes, or None if rename not possible.
        """
        if not self._initialized or self.process is None:
            return None

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return None

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": column - 1},
                "newName": new_name,
            },
        )

        return self._parse_workspace_edit(result)

    def prepare_rename(
        self, file: Path, line: int, column: int
    ) -> tuple[str, tuple[int, int], tuple[int, int]] | None:
        """Check if rename is valid and get the default range.

        Args:
            file: The file path
            line: 1-indexed line number
            column: 1-indexed column number

        Returns:
            Tuple of (placeholder_text, start_pos, end_pos) or None if not renameable.
            Positions are 1-indexed (line, column) tuples.
        """
        if not self._initialized or self.process is None:
            return None

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return None

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/prepareRename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": column - 1},
            },
        )

        if not result:
            return None

        # Result can be Range | { range: Range, placeholder: string }
        if "placeholder" in result:
            placeholder = result["placeholder"]
            range_obj = result.get("range", {})
        elif "start" in result:
            # Just a range, use empty placeholder
            placeholder = ""
            range_obj = result
        else:
            return None

        start = range_obj.get("start", {})
        end = range_obj.get("end", {})
        start_pos = (start.get("line", 0) + 1, start.get("character", 0) + 1)
        end_pos = (end.get("line", 0) + 1, end.get("character", 0) + 1)

        return (placeholder, start_pos, end_pos)

    def format_document(
        self, file: Path, tab_size: int = 4, insert_spaces: bool = True
    ) -> list[TextEdit] | None:
        """Format an entire document using LSP.

        Args:
            file: The file path to format
            tab_size: Number of spaces per tab
            insert_spaces: Use spaces instead of tabs

        Returns:
            List of TextEdit objects to apply, or None if formatting not available.
        """
        if not self._initialized or self.process is None:
            return None

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return None

        result = self._send_request(
            "textDocument/formatting",
            {
                "textDocument": {"uri": uri},
                "options": {
                    "tabSize": tab_size,
                    "insertSpaces": insert_spaces,
                },
            },
        )

        if not result:
            return None

        return self._parse_text_edits(file, result)

    def get_signature_help(
        self, file: Path, line: int, column: int
    ) -> SignatureInfo | None:
        """Get signature help for a function call.

        Args:
            file: The file path
            line: 1-indexed line number
            column: 1-indexed column number (should be inside function parentheses)

        Returns:
            SignatureInfo object or None if no signature help available.
        """
        if not self._initialized or self.process is None:
            return None

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return None

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/signatureHelp",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": column - 1},
            },
        )

        if result is None:
            return None

        return self._parse_signature_help(result)

    def get_inlay_hints(
        self, file: Path, start_line: int = 1, end_line: int | None = None
    ) -> list[InlayHint]:
        """Get inlay hints for a range of lines (Phase 5).

        Inlay hints show inline annotations like parameter names and type hints.

        Args:
            file: The file path
            start_line: 1-indexed start line (default: 1)
            end_line: 1-indexed end line (default: end of file)

        Returns:
            List of InlayHint objects.
        """
        if not self._initialized or self.process is None:
            return []

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return []

        # Read file to get line count if end_line not specified
        if end_line is None:
            try:
                with open(file) as f:
                    end_line = sum(1 for _ in f)
            except Exception:
                end_line = 1000  # Default to large range

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/inlayHint",
            {
                "textDocument": {"uri": uri},
                "range": {
                    "start": {"line": start_line - 1, "character": 0},
                    "end": {"line": end_line - 1, "character": 999},
                },
            },
        )

        if result is None:
            return []

        return self._parse_inlay_hints(result)

    def prepare_call_hierarchy(
        self, file: Path, line: int, column: int
    ) -> list[CallHierarchyItem]:
        """Prepare call hierarchy for a symbol (Phase 5).

        This is the first step in call hierarchy navigation.
        Returns items that can be used with get_incoming_calls/get_outgoing_calls.

        Args:
            file: The file path
            line: 1-indexed line number
            column: 1-indexed column number

        Returns:
            List of CallHierarchyItem objects (usually 1 item for the symbol).
        """
        if not self._initialized or self.process is None:
            return []

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return []

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": column - 1},
            },
        )

        if result is None:
            return []

        return self._parse_call_hierarchy_items(result)

    def _build_lsp_item_from_hierarchy_item(self, item: CallHierarchyItem) -> dict:
        """Build LSP CallHierarchyItem dict from our CallHierarchyItem dataclass.

        Args:
            item: A CallHierarchyItem from prepare_call_hierarchy

        Returns:
            Dict representing the LSP CallHierarchyItem structure.
        """
        return {
            "name": item.name,
            "kind": self._symbol_kind_to_int(item.kind),
            "uri": item.file.as_uri(),
            "range": {
                "start": {"line": item.line - 1, "character": item.column - 1},
                "end": {
                    "line": item.line - 1,
                    "character": item.column - 1 + len(item.name),
                },
            },
            "selectionRange": {
                "start": {"line": item.line - 1, "character": item.column - 1},
                "end": {
                    "line": item.line - 1,
                    "character": item.column - 1 + len(item.name),
                },
            },
            **item._data,  # Include any additional LSP data
        }

    def get_incoming_calls(self, item: CallHierarchyItem) -> list[CallHierarchyCall]:
        """Get functions/methods that call the given item (Phase 5).

        Args:
            item: A CallHierarchyItem from prepare_call_hierarchy

        Returns:
            List of CallHierarchyCall representing callers.
        """
        if not self._initialized or self.process is None:
            return []

        lsp_item = self._build_lsp_item_from_hierarchy_item(item)
        result = self._send_request("callHierarchy/incomingCalls", {"item": lsp_item})

        if result is None:
            return []

        return self._parse_call_hierarchy_calls(result, incoming=True)

    def get_outgoing_calls(self, item: CallHierarchyItem) -> list[CallHierarchyCall]:
        """Get functions/methods called by the given item (Phase 5).

        Args:
            item: A CallHierarchyItem from prepare_call_hierarchy

        Returns:
            List of CallHierarchyCall representing callees.
        """
        if not self._initialized or self.process is None:
            return []

        lsp_item = self._build_lsp_item_from_hierarchy_item(item)
        result = self._send_request("callHierarchy/outgoingCalls", {"item": lsp_item})

        if result is None:
            return []

        return self._parse_call_hierarchy_calls(result, incoming=False)

    def get_code_actions(
        self,
        file: Path,
        line: int,
        column: int,
        end_line: int | None = None,
        end_column: int | None = None,
    ) -> list[CodeAction]:
        """Get available code actions for a range.

        Args:
            file: The file path
            line: 1-indexed line number (start)
            column: 1-indexed column number (start)
            end_line: 1-indexed end line (optional, defaults to start line)
            end_column: 1-indexed end column (optional, defaults to start column)

        Returns:
            List of CodeAction objects available at the location.
        """
        if not self._initialized or self.process is None:
            return []

        try:
            uri = self._ensure_file_open(file)
        except Exception:
            return []

        # Default end position to start position
        if end_line is None:
            end_line = line
        if end_column is None:
            end_column = column

        # Get diagnostics for context (code actions often depend on diagnostics)
        diagnostics = self.get_diagnostics(file)
        diag_params = []
        for diag in diagnostics:
            if diag.line >= line and diag.line <= end_line:
                # Include diagnostic in the request
                diag_params.append(
                    {
                        "range": {
                            "start": {
                                "line": diag.line - 1,
                                "character": diag.column - 1,
                            },
                            "end": {"line": diag.line - 1, "character": diag.column},
                        },
                        "message": diag.message,
                        "severity": {
                            "error": 1,
                            "warning": 2,
                            "info": 3,
                            "hint": 4,
                        }.get(diag.severity, 1),
                        "source": diag.source,
                        "code": diag.code,
                    }
                )

        # LSP uses 0-indexed positions
        result = self._send_request(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": uri},
                "range": {
                    "start": {"line": line - 1, "character": column - 1},
                    "end": {"line": end_line - 1, "character": end_column - 1},
                },
                "context": {
                    "diagnostics": diag_params,
                    "triggerKind": 1,  # Invoked by user
                },
            },
        )

        return self._parse_code_actions(result, diagnostics)

    def get_workspace_symbols(self, query: str) -> list[SymbolInfo]:
        """Search for symbols across the workspace.

        Args:
            query: Search query (empty string returns all symbols)

        Returns:
            List of SymbolInfo objects matching the query.
        """
        if not self._initialized or self.process is None:
            return []

        result = self._send_request("workspace/symbol", {"query": query})

        return self._parse_workspace_symbols(result)


    # Phase 6 methods

    def get_semantic_tokens(
        self, file: Path, start_line: int | None = None, end_line: int | None = None
    ) -> list[SemanticToken]:
        """Get semantic tokens for syntax highlighting (Phase 6).

        Args:
            file: Path to the file
            start_line: Optional start line (1-indexed) for range request
            end_line: Optional end line (1-indexed) for range request

        Returns:
            List of SemanticToken for the file/range.
        """
        if not self._initialized or self.process is None:
            return []

        uri = self._ensure_file_open(file)
        params: dict[str, Any] = {"textDocument": {"uri": uri}}

        # Use range request if both start and end specified
        if start_line is not None and end_line is not None:
            params["range"] = {
                "start": {"line": start_line - 1, "character": 0},
                "end": {"line": end_line, "character": 0},
            }
            result = self._send_request("textDocument/semanticTokens/range", params)
        else:
            result = self._send_request("textDocument/semanticTokens/full", params)

        if result is None:
            return []

        return self._parse_semantic_tokens(result)

    def get_document_links(self, file: Path) -> list[DocumentLink]:
        """Get clickable links in a document (Phase 6).

        Returns links to files, URLs, etc. that appear in the document.

        Args:
            file: Path to the file

        Returns:
            List of DocumentLink in the file.
        """
        if not self._initialized or self.process is None:
            return []

        uri = self._ensure_file_open(file)
        result = self._send_request(
            "textDocument/documentLink", {"textDocument": {"uri": uri}}
        )

        if result is None:
            return []

        return self._parse_document_links(result)

    def get_code_lenses(self, file: Path) -> list[CodeLens]:
        """Get code lenses for a document (Phase 6).

        Code lenses are actionable annotations displayed above code,
        like "5 references", "Run test", etc.

        Args:
            file: Path to the file

        Returns:
            List of CodeLens in the file.
        """
        if not self._initialized or self.process is None:
            return []

        uri = self._ensure_file_open(file)
        result = self._send_request(
            "textDocument/codeLens", {"textDocument": {"uri": uri}}
        )

        if result is None:
            return []

        return self._parse_code_lenses(result)

    def _parse_text_edits(self, file: Path, result: list[dict]) -> list[TextEdit]:
        """Parse LSP TextEdit[] response into TextEdit objects."""
        edits: list[TextEdit] = []
        for edit in result:
            range_obj = edit.get("range", {})
            start = range_obj.get("start", {})
            end = range_obj.get("end", {})
            new_text = edit.get("newText", "")

            edits.append(
                TextEdit(
                    file=file,
                    start_line=start.get("line", 0) + 1,
                    start_column=start.get("character", 0) + 1,
                    end_line=end.get("line", 0) + 1,
                    end_column=end.get("character", 0) + 1,
                    new_text=new_text,
                )
            )
        return edits

    def _parse_signature_help(self, result: dict) -> SignatureInfo | None:
        """Parse LSP SignatureHelp response into SignatureInfo object."""
        if not result:
            return None

        signatures_data = result.get("signatures", [])
        if not signatures_data:
            return None

        signatures: list[SignatureLabel] = []
        for sig_data in signatures_data:
            label = sig_data.get("label", "")
            doc = sig_data.get("documentation")
            if isinstance(doc, dict):
                doc = doc.get("value", None)

            # Parse parameters
            params: list[SignatureParameter] = []
            for param_data in sig_data.get("parameters", []):
                param_label = param_data.get("label", "")
                if isinstance(param_label, list) and len(param_label) == 2:
                    # Label is [start, end] indices into signature label
                    start, end = param_label
                    param_label = label[start:end]
                param_doc = param_data.get("documentation")
                if isinstance(param_doc, dict):
                    param_doc = param_doc.get("value", None)
                params.append(
                    SignatureParameter(label=str(param_label), documentation=param_doc)
                )

            signatures.append(
                SignatureLabel(label=label, documentation=doc, parameters=params)
            )

        return SignatureInfo(
            signatures=signatures,
            active_signature=result.get("activeSignature", 0),
            active_parameter=result.get("activeParameter"),
        )

    def _parse_workspace_edit(self, result: Any) -> WorkspaceEdit | None:
        """Parse LSP WorkspaceEdit response into WorkspaceEdit object."""
        if not result:
            return None

        edits: list[TextEdit] = []

        # Handle "changes" format: { uri: TextEdit[] }
        changes = result.get("changes", {})
        for uri, text_edits in changes.items():
            # Parse URI to file path

            parsed = urlparse(uri)
            file_path = Path(unquote(parsed.path))

            for edit in text_edits:
                range_obj = edit.get("range", {})
                start = range_obj.get("start", {})
                end = range_obj.get("end", {})
                new_text = edit.get("newText", "")

                edits.append(
                    TextEdit(
                        file=file_path,
                        start_line=start.get("line", 0) + 1,
                        start_column=start.get("character", 0) + 1,
                        end_line=end.get("line", 0) + 1,
                        end_column=end.get("character", 0) + 1,
                        new_text=new_text,
                    )
                )

        # Handle "documentChanges" format (more complex, includes versioning)
        doc_changes = result.get("documentChanges", [])
        for doc_change in doc_changes:
            # TextDocumentEdit
            if "textDocument" in doc_change and "edits" in doc_change:
                uri = doc_change["textDocument"].get("uri", "")

                parsed = urlparse(uri)
                file_path = Path(unquote(parsed.path))

                for edit in doc_change["edits"]:
                    range_obj = edit.get("range", {})
                    start = range_obj.get("start", {})
                    end = range_obj.get("end", {})
                    new_text = edit.get("newText", "")

                    edits.append(
                        TextEdit(
                            file=file_path,
                            start_line=start.get("line", 0) + 1,
                            start_column=start.get("character", 0) + 1,
                            end_line=end.get("line", 0) + 1,
                            end_column=end.get("character", 0) + 1,
                            new_text=new_text,
                        )
                    )

        if not edits:
            return None

        return WorkspaceEdit(edits=edits)

    def _parse_locations(self, result: Any) -> list[Location]:
        """Parse LSP location response into Location objects."""
        if result is None:
            return []

        locations: list[Location] = []

        # Result can be Location, Location[], or LocationLink[]
        if isinstance(result, dict):
            result = [result]

        for item in result:
            # Handle LocationLink (has targetUri) vs Location (has uri)
            if "targetUri" in item:
                uri = item["targetUri"]
                range_info = item.get(
                    "targetRange", item.get("targetSelectionRange", {})
                )
            else:
                uri = item.get("uri", "")
                range_info = item.get("range", {})

            # Parse URI to file path
            parsed = urlparse(uri)
            file_path = Path(unquote(parsed.path))

            start = range_info.get("start", {})
            end = range_info.get("end", {})

            locations.append(
                Location(
                    file=file_path,
                    line=start.get("line", 0) + 1,  # 0-indexed to 1-indexed
                    column=start.get("character", 0) + 1,
                    end_line=end.get("line", 0) + 1 if end else None,
                    end_column=end.get("character", 0) + 1 if end else None,
                )
            )

        return locations

    def _parse_hover(self, result: dict) -> HoverInfo | None:
        """Parse LSP hover response into HoverInfo object."""
        if not result:
            return None

        contents = result.get("contents")
        if contents is None:
            return None

        # Contents can be MarkedString | MarkedString[] | MarkupContent
        content_str = ""
        if isinstance(contents, str):
            content_str = contents
        elif isinstance(contents, dict):
            # MarkupContent: { kind: "markdown"|"plaintext", value: str }
            # or MarkedString: { language: str, value: str }
            if "value" in contents:
                content_str = contents["value"]
                # Add language fence if it's a code block
                if "language" in contents and contents["language"]:
                    content_str = f"```{contents['language']}\n{content_str}\n```"
        elif isinstance(contents, list):
            # Array of MarkedString
            parts = []
            for item in contents:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and "value" in item:
                    val = item["value"]
                    if "language" in item and item["language"]:
                        val = f"```{item['language']}\n{val}\n```"
                    parts.append(val)
            content_str = "\n\n".join(parts)

        if not content_str.strip():
            return None

        # Parse range if present
        range_start = None
        range_end = None
        if "range" in result:
            r = result["range"]
            start = r.get("start", {})
            end = r.get("end", {})
            range_start = (start.get("line", 0) + 1, start.get("character", 0) + 1)
            range_end = (end.get("line", 0) + 1, end.get("character", 0) + 1)

        return HoverInfo(
            contents=content_str.strip(),
            range_start=range_start,
            range_end=range_end,
        )

    def _parse_inlay_hints(self, result: list[dict]) -> list[InlayHint]:
        """Parse LSP inlayHint response into InlayHint objects (Phase 5)."""
        hints: list[InlayHint] = []
        for hint in result:
            position = hint.get("position", {})
            line = position.get("line", 0) + 1
            column = position.get("character", 0) + 1

            # Label can be string or InlayHintLabelPart[]
            label_data = hint.get("label", "")
            if isinstance(label_data, str):
                label = label_data
            elif isinstance(label_data, list):
                # Array of parts, concatenate their values
                label = "".join(p.get("value", "") for p in label_data)
            else:
                label = str(label_data)

            # Kind: 1 = Type, 2 = Parameter
            kind_map = {1: "type", 2: "parameter"}
            hint_kind = hint.get("kind")
            kind = kind_map.get(hint_kind) if isinstance(hint_kind, int) else None

            hints.append(
                InlayHint(
                    line=line,
                    column=column,
                    label=label,
                    kind=kind,
                    padding_left=hint.get("paddingLeft", False),
                    padding_right=hint.get("paddingRight", False),
                )
            )
        return hints

    def _parse_call_hierarchy_items(
        self, result: list[dict]
    ) -> list[CallHierarchyItem]:
        """Parse LSP prepareCallHierarchy response into CallHierarchyItem objects (Phase 5)."""
        items: list[CallHierarchyItem] = []
        for item in result:
            name = item.get("name", "")
            kind_int = item.get("kind", 0)
            kind = self._symbol_kind_from_int(kind_int)

            uri = item.get("uri", "")
            file = (
                Path(uri.replace("file://", ""))
                if uri.startswith("file://")
                else Path(uri)
            )

            range_obj = item.get("selectionRange", item.get("range", {}))
            start = range_obj.get("start", {})
            line = start.get("line", 0) + 1
            column = start.get("character", 0) + 1

            detail = item.get("detail")

            # Store full item data for follow-up requests
            data = {
                k: v
                for k, v in item.items()
                if k not in ("name", "kind", "uri", "detail")
            }

            items.append(
                CallHierarchyItem(
                    name=name,
                    kind=kind,
                    file=file,
                    line=line,
                    column=column,
                    detail=detail,
                    _data=data,
                )
            )
        return items

    def _parse_call_hierarchy_calls(
        self, result: list[dict], incoming: bool = True
    ) -> list[CallHierarchyCall]:
        """Parse LSP callHierarchy/incomingCalls or outgoingCalls response (Phase 5)."""
        calls: list[CallHierarchyCall] = []
        for call in result:
            # Key is "from" for incoming, "to" for outgoing
            item_key = "from" if incoming else "to"
            item_data = call.get(item_key, {})

            # Parse the item
            items = self._parse_call_hierarchy_items([item_data])
            if not items:
                continue
            item = items[0]

            # Parse fromRanges (locations where the call occurs)
            from_ranges: list[tuple[int, int]] = []
            for range_obj in call.get("fromRanges", []):
                start = range_obj.get("start", {})
                from_ranges.append(
                    (start.get("line", 0) + 1, start.get("character", 0) + 1)
                )

            calls.append(CallHierarchyCall(item=item, from_ranges=from_ranges))
        return calls

    def _symbol_kind_from_int(self, kind: int) -> str:
        """Convert LSP SymbolKind integer to string (Phase 5)."""
        kind_map = {
            1: "file",
            2: "module",
            3: "namespace",
            4: "package",
            5: "class",
            6: "method",
            7: "property",
            8: "field",
            9: "constructor",
            10: "enum",
            11: "interface",
            12: "function",
            13: "variable",
            14: "constant",
            15: "string",
            16: "number",
            17: "boolean",
            18: "array",
            19: "object",
            20: "key",
            21: "null",
            22: "enummember",
            23: "struct",
            24: "event",
            25: "operator",
            26: "typeparameter",
        }
        return kind_map.get(kind, "unknown")

    def _symbol_kind_to_int(self, kind: str) -> int:
        """Convert symbol kind string to LSP SymbolKind integer (Phase 5)."""
        kind_map = {
            "file": 1,
            "module": 2,
            "namespace": 3,
            "package": 4,
            "class": 5,
            "method": 6,
            "property": 7,
            "field": 8,
            "constructor": 9,
            "enum": 10,
            "interface": 11,
            "function": 12,
            "variable": 13,
            "constant": 14,
            "string": 15,
            "number": 16,
            "boolean": 17,
            "array": 18,
            "object": 19,
            "key": 20,
            "null": 21,
            "enummember": 22,
            "struct": 23,
            "event": 24,
            "operator": 25,
            "typeparameter": 26,
        }
        return kind_map.get(kind.lower(), 12)  # Default to function

    def _parse_code_actions(
        self, result: Any, diagnostics: list[Diagnostic]
    ) -> list[CodeAction]:
        """Parse LSP codeAction response into CodeAction objects."""
        if not result:
            return []

        actions = []
        for action in result:
            if isinstance(action, dict):
                title = action.get("title", "Unknown action")
                kind = action.get("kind")
                is_preferred = action.get("isPreferred", False)

                # Parse edit if present
                edit = None
                if "edit" in action:
                    edit = self._parse_workspace_edit(action["edit"])

                # Parse associated diagnostics
                action_diags = None
                if "diagnostics" in action:
                    action_diags = [
                        d
                        for d in diagnostics
                        if any(
                            ad.get("message") == d.message
                            for ad in action.get("diagnostics", [])
                        )
                    ]

                actions.append(
                    CodeAction(
                        title=title,
                        kind=kind,
                        edit=edit,
                        diagnostics=action_diags,
                        is_preferred=is_preferred,
                    )
                )

        return actions

    def _parse_workspace_symbols(self, result: Any) -> list[SymbolInfo]:
        """Parse LSP workspace/symbol response into SymbolInfo objects."""
        if not result:
            return []

        # Symbol kind mapping from LSP spec
        symbol_kinds = {
            1: "file",
            2: "module",
            3: "namespace",
            4: "package",
            5: "class",
            6: "method",
            7: "property",
            8: "field",
            9: "constructor",
            10: "enum",
            11: "interface",
            12: "function",
            13: "variable",
            14: "constant",
            15: "string",
            16: "number",
            17: "boolean",
            18: "array",
            19: "object",
            20: "key",
            21: "null",
            22: "enum_member",
            23: "struct",
            24: "event",
            25: "operator",
            26: "type_parameter",
        }

        symbols = []
        for sym in result:
            if isinstance(sym, dict):
                name = sym.get("name", "")
                kind_num = sym.get("kind", 0)
                kind = symbol_kinds.get(kind_num, "unknown")
                container = sym.get("containerName")

                # Parse location
                location_data = sym.get("location", {})
                uri = location_data.get("uri", "")
                range_obj = location_data.get("range", {})

                if uri:
                    parsed = urlparse(uri)
                    file_path = Path(unquote(parsed.path))

                    start = range_obj.get("start", {})
                    end = range_obj.get("end", {})

                    location = Location(
                        file=file_path,
                        line=start.get("line", 0) + 1,
                        column=start.get("character", 0) + 1,
                        end_line=end.get("line", 0) + 1,
                        end_column=end.get("character", 0) + 1,
                    )

                    symbols.append(
                        SymbolInfo(
                            name=name,
                            kind=kind,
                            location=location,
                            container_name=container,
                        )
                    )

        return symbols

    # Phase 6 parser methods

    def _parse_semantic_tokens(self, result: dict) -> list[SemanticToken]:
        """Parse LSP semanticTokens response (Phase 6).

        The data is encoded as a flat array where each token is 5 integers:
        [deltaLine, deltaStartChar, length, tokenType, tokenModifiers]
        """
        tokens: list[SemanticToken] = []
        data = result.get("data", [])

        if not data:
            return tokens

        # Get token type and modifier legends from server capabilities
        # These should have been stored during initialization
        token_types = getattr(self, "_semantic_token_types", [])
        token_modifiers = getattr(self, "_semantic_token_modifiers", [])

        # Default legends if not available
        if not token_types:
            token_types = [
                "namespace",
                "type",
                "class",
                "enum",
                "interface",
                "struct",
                "typeParameter",
                "parameter",
                "variable",
                "property",
                "enumMember",
                "event",
                "function",
                "method",
                "macro",
                "keyword",
                "modifier",
                "comment",
                "string",
                "number",
                "regexp",
                "operator",
            ]

        current_line = 0
        current_char = 0

        # Process tokens in groups of 5
        for i in range(0, len(data), 5):
            if i + 4 >= len(data):
                break

            delta_line = data[i]
            delta_char = data[i + 1]
            length = data[i + 2]
            token_type_idx = data[i + 3]
            modifier_bits = data[i + 4]

            # Update position
            if delta_line > 0:
                current_line += delta_line
                current_char = delta_char
            else:
                current_char += delta_char

            # Get token type name
            token_type = (
                token_types[token_type_idx]
                if token_type_idx < len(token_types)
                else f"type{token_type_idx}"
            )

            # Decode modifiers (bitmask)
            modifiers: list[str] = []
            for j, mod_name in enumerate(token_modifiers):
                if modifier_bits & (1 << j):
                    modifiers.append(mod_name)

            tokens.append(
                SemanticToken(
                    line=current_line + 1,  # Convert to 1-indexed
                    column=current_char + 1,
                    length=length,
                    token_type=token_type,
                    modifiers=modifiers,
                )
            )

        return tokens

    def _parse_document_links(self, result: list[dict]) -> list[DocumentLink]:
        """Parse LSP documentLink response (Phase 6)."""
        links: list[DocumentLink] = []

        for link_data in result:
            range_obj = link_data.get("range", {})
            start = range_obj.get("start", {})
            end = range_obj.get("end", {})

            target = link_data.get("target")
            tooltip = link_data.get("tooltip")

            links.append(
                DocumentLink(
                    start_line=start.get("line", 0) + 1,
                    start_column=start.get("character", 0) + 1,
                    end_line=end.get("line", 0) + 1,
                    end_column=end.get("character", 0) + 1,
                    target=target,
                    tooltip=tooltip,
                )
            )

        return links

    def _parse_code_lenses(self, result: list[dict]) -> list[CodeLens]:
        """Parse LSP codeLens response (Phase 6)."""
        lenses: list[CodeLens] = []

        for lens_data in result:
            range_obj = lens_data.get("range", {})
            start = range_obj.get("start", {})

            command = lens_data.get("command")
            command_title = command.get("title") if command else None
            command_id = command.get("command") if command else None
            command_args = command.get("arguments") if command else None

            lenses.append(
                CodeLens(
                    line=start.get("line", 0) + 1,
                    column=start.get("character", 0) + 1,
                    command_title=command_title,
                    command_id=command_id,
                    command_args=command_args,
                )
            )

        return lenses


    def _guess_language_id(self, file: Path) -> str:
        """Guess the LSP language ID from file extension."""
        ext_map = {
            ".py": "python",
            ".pyi": "python",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".go": "go",
            ".rs": "rust",
            ".c": "c",
            ".cpp": "cpp",
            ".h": "c",
            ".hpp": "cpp",
        }
        return ext_map.get(file.suffix.lower(), "plaintext")


# Language server configurations
# Users can add more via config
# Note: Default servers are now defined in config.py
# This is kept for backward compatibility but load_config() is preferred
KNOWN_SERVERS: dict[str, list[str]] = {
    "python": ["pyright-langserver", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "javascript": ["typescript-language-server", "--stdio"],
    "go": ["gopls"],
    "rust": ["rust-analyzer"],
}


def detect_language_servers(workspace: Path) -> list[tuple[str, list[str]]]:
    """Detect which language servers are needed based on workspace files.

    Returns a list of (language, command) tuples.
    """
    detected = []

    # Check for Python
    if list(workspace.rglob("*.py"))[:1] or (workspace / "pyproject.toml").exists():
        if _is_server_available("pyright-langserver"):
            detected.append(("python", KNOWN_SERVERS["python"]))

    # Check for TypeScript/JavaScript
    if (
        list(workspace.rglob("*.ts"))[:1]
        or list(workspace.rglob("*.tsx"))[:1]
        or (workspace / "package.json").exists()
    ):
        if _is_server_available("typescript-language-server"):
            detected.append(("typescript", KNOWN_SERVERS["typescript"]))

    # Check for Go
    if list(workspace.rglob("*.go"))[:1] or (workspace / "go.mod").exists():
        if _is_server_available("gopls"):
            detected.append(("go", KNOWN_SERVERS["go"]))

    # Check for Rust
    if list(workspace.rglob("*.rs"))[:1] or (workspace / "Cargo.toml").exists():
        if _is_server_available("rust-analyzer"):
            detected.append(("rust", KNOWN_SERVERS["rust"]))

    return detected


def _is_server_available(command: str) -> bool:
    """Check if a language server command is available."""
    return shutil.which(command) is not None


class LSPManager:
    """Manages multiple language servers for a workspace.

    Features:
    - Lazy initialization: Servers start only when first needed
    - Config-based: Custom server paths via .gptme-lsp.toml
    - Auto-restart: Crashed servers restart on next command
    """

    def __init__(self, workspace: Path, lazy: bool = True):
        """Initialize the LSP manager.

        Args:
            workspace: Root directory for the workspace
            lazy: If True (default), servers start only when needed.
                  If False, auto-detect and start servers immediately.
        """
        self.workspace = workspace
        self.servers: dict[str, LSPServer] = {}
        self._config = load_config(workspace)
        self._lazy = lazy

        if not lazy:
            self.start_detected_servers()

    def start_detected_servers(self) -> list[str]:
        """Auto-detect and start appropriate language servers.

        Returns list of started server names.
        """
        detected = detect_language_servers(self.workspace)
        started = []

        for language, command in detected:
            if language not in self.servers:
                # Use config command if available, otherwise use detected
                config_cmd = self._config.get(language)
                server = LSPServer(
                    name=language,
                    command=config_cmd if config_cmd else command,
                    workspace=self.workspace,
                )
                if server.start():
                    self.servers[language] = server
                    started.append(language)
                else:
                    # Log helpful error message
                    logger.warning(format_server_error(language, "start_failed"))

        return started

    def start_server(self, language: str, command: list[str] | None = None) -> bool:
        """Start a specific language server."""
        if language in self.servers:
            server = self.servers[language]
            # Check if server is still alive
            if server.process and server.process.poll() is None:
                return True
            # Server died, remove and restart
            logger.info(f"Restarting crashed {language} server")
            del self.servers[language]

        if command is None:
            # Try config first, then known servers
            command = self._config.get(language) or KNOWN_SERVERS.get(language)
            if command is None:
                logger.error(format_server_error(language, "not_found"))
                return False

        server = LSPServer(
            name=language,
            command=command,
            workspace=self.workspace,
        )
        if server.start():
            self.servers[language] = server
            return True

        logger.error(format_server_error(language, "start_failed"))
        return False

    def _ensure_server(self, language: str) -> LSPServer | None:
        """Ensure a server is running for the given language (lazy init).

        Returns the server if available, None otherwise.
        """
        if language not in self.servers:
            if not self.start_server(language):
                return None
        return self.servers.get(language)

    def stop_all(self) -> None:
        """Stop all language servers."""
        for server in self.servers.values():
            server.stop()
        self.servers.clear()

    def get_diagnostics(self, file: Path) -> list[Diagnostic]:
        """Get diagnostics for a file from the appropriate server.

        Lazily starts the server if not already running.
        """
        language = self._file_to_language(file)
        if language is None:
            return []

        server = self._ensure_server(language)
        if server is None:
            return []

        return server.get_diagnostics(file)

    def get_definition(self, file: Path, line: int, column: int) -> list[Location]:
        """Get definition location (lazy init)."""
        language = self._file_to_language(file)
        if language is None:
            return []

        server = self._ensure_server(language)
        if server is None:
            return []

        return server.get_definition(file, line, column)

    def get_references(self, file: Path, line: int, column: int) -> list[Location]:
        """Get references (lazy init)."""
        language = self._file_to_language(file)
        if language is None:
            return []

        server = self._ensure_server(language)
        if server is None:
            return []

        return server.get_references(file, line, column)

    def get_hover(self, file: Path, line: int, column: int) -> HoverInfo | None:
        """Get hover info (lazy init)."""
        language = self._file_to_language(file)
        if language is None:
            return None

        server = self._ensure_server(language)
        if server is None:
            return None

        return server.get_hover(file, line, column)

    def format_document(
        self, file: Path, tab_size: int = 4, insert_spaces: bool = True
    ) -> list[TextEdit] | None:
        """Format a document (lazy init)."""
        language = self._file_to_language(file)
        if language is None:
            return None

        server = self._ensure_server(language)
        if server is None:
            return None

        return server.format_document(file, tab_size, insert_spaces)

    def get_signature_help(
        self, file: Path, line: int, column: int
    ) -> SignatureInfo | None:
        """Get signature help (lazy init)."""
        language = self._file_to_language(file)
        if language is None:
            return None

        server = self._ensure_server(language)
        if server is None:
            return None

        return server.get_signature_help(file, line, column)

    def get_inlay_hints(
        self, file: Path, start_line: int = 1, end_line: int | None = None
    ) -> list[InlayHint]:
        """Get inlay hints for a file range (Phase 5, lazy init)."""
        language = self._file_to_language(file)
        if language is None:
            return []

        server = self._ensure_server(language)
        if server is None:
            return []

        return server.get_inlay_hints(file, start_line, end_line)

    def prepare_call_hierarchy(
        self, file: Path, line: int, column: int
    ) -> list[CallHierarchyItem]:
        """Prepare call hierarchy for a symbol (Phase 5, lazy init)."""
        language = self._file_to_language(file)
        if language is None:
            return []

        server = self._ensure_server(language)
        if server is None:
            return []

        return server.prepare_call_hierarchy(file, line, column)

    def get_incoming_calls(self, item: CallHierarchyItem) -> list[CallHierarchyCall]:
        """Get callers of a symbol (Phase 5, lazy init)."""
        language = self._file_to_language(item.file)
        if language is None:
            return []

        server = self._ensure_server(language)
        if server is None:
            return []

        return server.get_incoming_calls(item)

    def get_outgoing_calls(self, item: CallHierarchyItem) -> list[CallHierarchyCall]:
        """Get callees of a symbol (Phase 5, lazy init)."""
        language = self._file_to_language(item.file)
        if language is None:
            return []

        server = self._ensure_server(language)
        if server is None:
            return []

        return server.get_outgoing_calls(item)

    def _file_to_language(self, file: Path) -> str | None:
        """Map file to language server."""
        ext_map = {
            ".py": "python",
            ".pyi": "python",
            ".js": "typescript",  # Use TS server for JS
            ".jsx": "typescript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".go": "go",
            ".rs": "rust",
        }
        return ext_map.get(file.suffix.lower())
