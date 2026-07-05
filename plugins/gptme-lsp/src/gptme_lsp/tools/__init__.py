"""LSP tools for gptme.

Provides tools for interacting with Language Server Protocol servers.
The tool is automatically discovered by gptme's plugin system.
"""

from .lsp_tool import tool

__all__ = ["tool"]
