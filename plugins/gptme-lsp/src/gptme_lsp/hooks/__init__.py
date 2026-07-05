"""LSP hooks for gptme.

Provides hooks for automatic diagnostics after file saves.
"""

from .post_save import register

__all__ = ["register"]
