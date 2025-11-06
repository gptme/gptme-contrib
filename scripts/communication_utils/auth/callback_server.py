"""
OAuth callback server for local OAuth flows.

Provides a lightweight Flask server to handle OAuth callbacks
during authorization flows. Supports configurable ports and paths.
"""

import html
import threading
from queue import Queue, Empty
from typing import Optional

from flask import Flask, request
from werkzeug.serving import BaseWSGIServer, make_server


class CallbackServer:
    """
    Lightweight OAuth callback server for local development.

    Runs a Flask server in a background thread to receive OAuth
    callbacks at a configurable localhost URL.
    """

    def __init__(
        self,
        port: int = 8080,
        callback_path: str = "/callback",
        timeout: int = 300,
    ):
        """
        Initialize callback server.

        Args:
            port: Port to run server on (default: 8080)
            callback_path: URL path for callback (default: /callback)
            timeout: Seconds to wait for callback (default: 300)
        """
        self.port = port
        self.callback_path = callback_path
        self.timeout = timeout

        self.app = Flask(__name__)
        self.server: Optional[BaseWSGIServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.code_queue: Queue = Queue()

        # Register routes
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Setup Flask routes for callback handling."""

        @self.app.route(self.callback_path)
        def callback():
            """Handle OAuth callback"""
            code = request.args.get("code")
            if code:
                # Store full URL for platforms that need it
                full_url = request.url
                # Some platforms (like Twitter) need https in callback URL
                # even when running locally
                full_url = full_url.replace("http://", "https://")
                self.code_queue.put((code, full_url))

                return """
                <h1>Authorization Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                <script>setTimeout(function() { window.close(); }, 1000);</script>
                """

            error = request.args.get("error", "Unknown error")
            error_description = request.args.get("error_description", "Unknown error")

            # Escape HTML to prevent XSS attacks
            error_safe = html.escape(error)
            error_description_safe = html.escape(error_description)

            return (
                f"""
                <h1>Authorization Failed</h1>
                <p>Error: {error_safe}</p>
                <p>Description: {error_description_safe}</p>
                <p>You can close this window and return to the terminal.</p>
                """,
                400,
            )

    def start(self) -> None:
        """Start the callback server in a background thread."""
        if self.server_thread and self.server_thread.is_alive():
            return  # Already running

        self.server = make_server("localhost", self.port, self.app, threaded=True)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()

    def stop(self) -> None:
        """Stop the callback server and wait for cleanup."""
        if self.server:
            self.server.shutdown()

        # Wait for server thread to finish before cleanup
        if self.server_thread:
            self.server_thread.join(timeout=5)
            if self.server_thread.is_alive():
                # Thread didn't finish - log warning but continue
                print("Warning: Server thread did not stop within timeout")

        # Clean up after thread has finished
        self.server = None
        self.server_thread = None

    def wait_for_callback(
        self, timeout: Optional[int] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Wait for OAuth callback with authorization code.

        Args:
            timeout: Seconds to wait (default: use instance timeout)

        Returns:
            Tuple of (authorization_code, full_callback_url)
            Returns (None, None) on timeout
        """
        timeout = timeout or self.timeout

        try:
            code, full_url = self.code_queue.get(timeout=timeout)
            return code, full_url
        except Empty:
            return None, None

    def get_redirect_uri(self) -> str:
        """
        Get the redirect URI for OAuth configuration.

        Returns:
            Full redirect URI (e.g., http://localhost:8080/callback)
        """
        return f"http://localhost:{self.port}{self.callback_path}"

    def __enter__(self):
        """Context manager entry - start server."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - stop server."""
        self.stop()


def run_oauth_callback(
    port: int = 8080, callback_path: str = "/callback", timeout: int = 300
) -> tuple[Optional[str], Optional[str]]:
    """
    Convenience function to run callback server and wait for code.

    Args:
        port: Port to run server on
        callback_path: URL path for callback
        timeout: Seconds to wait for callback

    Returns:
        Tuple of (authorization_code, full_callback_url)

    Example:
        >>> with CallbackServer(port=9876) as server:
        ...     print(f"Redirect URI: {server.get_redirect_uri()}")
        ...     # User authorizes in browser
        ...     code, url = server.wait_for_callback()
        ...     print(f"Got code: {code}")
    """
    server = CallbackServer(port=port, callback_path=callback_path, timeout=timeout)
    with server:
        result: tuple[Optional[str], Optional[str]] = server.wait_for_callback()
        return result
