"""LSP plugin configuration management.

Supports loading custom language server configurations from:
1. Project-level: gptme.toml in workspace root
2. User-level: ~/.config/gptme/config.toml

Configuration format (using plugin.lsp namespace):
```toml
[plugin.lsp.servers]
python = ["pyright-langserver", "--stdio"]
typescript = ["typescript-language-server", "--stdio"]
go = ["gopls", "serve"]
rust = ["rust-analyzer"]

# Custom server
mypy = ["dmypy", "run", "--"]
```
"""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default server configurations
DEFAULT_SERVERS: dict[str, list[str]] = {
    "python": ["pyright-langserver", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "javascript": ["typescript-language-server", "--stdio"],
    "go": ["gopls"],
    "rust": ["rust-analyzer"],
}

# Installation hints for common servers
INSTALL_HINTS: dict[str, str] = {
    "python": "pip install pyright",
    "typescript": "npm install -g typescript-language-server typescript",
    "javascript": "npm install -g typescript-language-server typescript",
    "go": "go install golang.org/x/tools/gopls@latest",
    "rust": "rustup component add rust-analyzer",
}


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file, returning empty dict on failure."""
    if not path.exists():
        return {}

    try:
        # Python 3.11+ has tomllib in stdlib
        import tomllib

        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        # Fall back to toml package if available
        try:
            import toml  # type: ignore[import-untyped]

            result: dict[str, Any] = toml.load(path)
            return result
        except ImportError:
            logger.debug("No TOML parser available (need Python 3.11+ or toml package)")
            return {}
    except Exception as e:
        logger.warning(f"Failed to load config from {path}: {e}")
        return {}


def load_config(workspace: Path) -> dict[str, list[str]]:
    """Load LSP server configuration.

    Searches for config in order (later overrides earlier):
    1. Default built-in servers
    2. User config: ~/.config/gptme/config.toml [plugin.lsp.servers]
    3. Project config: gptme.toml in workspace root [plugin.lsp.servers]

    Returns:
        Dict mapping language name to server command list
    """
    servers = DEFAULT_SERVERS.copy()

    # User-level config
    user_config_path = Path.home() / ".config" / "gptme" / "config.toml"
    user_config = _load_toml(user_config_path)
    plugin_lsp = user_config.get("plugin", {}).get("lsp", {})
    if "servers" in plugin_lsp:
        logger.debug(f"Loaded user LSP config from {user_config_path}")
        servers.update(plugin_lsp["servers"])

    # Project-level config (overrides user)
    project_config_path = workspace / "gptme.toml"
    project_config = _load_toml(project_config_path)
    plugin_lsp = project_config.get("plugin", {}).get("lsp", {})
    if "servers" in plugin_lsp:
        logger.info(f"Loaded project LSP config from {project_config_path}")
        servers.update(plugin_lsp["servers"])

    return servers


def get_install_hint(language: str) -> str | None:
    """Get installation hint for a language server.

    Returns human-readable installation command or None if unknown.
    """
    return INSTALL_HINTS.get(language)


def format_server_error(
    language: str,
    error_type: str,
    details: str | None = None,
) -> str:
    """Format a helpful error message for LSP server issues.

    Args:
        language: The language (e.g., "python", "typescript")
        error_type: Type of error ("not_found", "start_failed", "timeout", "crash")
        details: Additional error details

    Returns:
        User-friendly error message with hints
    """
    hint = get_install_hint(language)

    if error_type == "not_found":
        msg = f"LSP server for {language} not found."
        if hint:
            msg += f"\n  → Install with: {hint}"
        return msg

    elif error_type == "start_failed":
        msg = f"Failed to start {language} language server."
        if details:
            msg += f"\n  → Error: {details}"
        if hint:
            msg += f"\n  → Verify installation: {hint}"
        return msg

    elif error_type == "timeout":
        msg = f"Timeout waiting for {language} language server response."
        msg += "\n  → The server may be overloaded or crashed."
        msg += "\n  → Try restarting gptme or check server logs."
        return msg

    elif error_type == "crash":
        msg = f"The {language} language server crashed unexpectedly."
        if details:
            msg += f"\n  → Error: {details}"
        msg += "\n  → The server will be restarted on next command."
        return msg

    else:
        msg = f"LSP error for {language}: {error_type}"
        if details:
            msg += f"\n  → {details}"
        return msg
