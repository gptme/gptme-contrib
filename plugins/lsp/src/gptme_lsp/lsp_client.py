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
            from urllib.parse import unquote, urlparse

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

        # Open the file (triggers diagnostics)
        uri = file.as_uri()
        try:
            content = file.read_text()
        except Exception as e:
            logger.error(f"Cannot read file {file}: {e}")
            return []

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
    """Manages multiple language servers for a workspace."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.servers: dict[str, LSPServer] = {}

    def start_detected_servers(self) -> list[str]:
        """Auto-detect and start appropriate language servers.

        Returns list of started server names.
        """
        detected = detect_language_servers(self.workspace)
        started = []

        for language, command in detected:
            if language not in self.servers:
                server = LSPServer(
                    name=language,
                    command=command,
                    workspace=self.workspace,
                )
                if server.start():
                    self.servers[language] = server
                    started.append(language)

        return started

    def start_server(self, language: str, command: list[str] | None = None) -> bool:
        """Start a specific language server."""
        if language in self.servers:
            return True

        if command is None:
            command = KNOWN_SERVERS.get(language)
            if command is None:
                logger.error(f"Unknown language server: {language}")
                return False

        server = LSPServer(
            name=language,
            command=command,
            workspace=self.workspace,
        )
        if server.start():
            self.servers[language] = server
            return True
        return False

    def stop_all(self) -> None:
        """Stop all language servers."""
        for server in self.servers.values():
            server.stop()
        self.servers.clear()

    def get_diagnostics(self, file: Path) -> list[Diagnostic]:
        """Get diagnostics for a file from the appropriate server."""
        language = self._file_to_language(file)
        if language is None:
            return []

        server = self.servers.get(language)
        if server is None:
            return []

        return server.get_diagnostics(file)

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
