"""
Ralph Loop plugin for gptme.

Implements iterative execution loops with context reset between steps.
Progress persists in files/git, not in LLM context.

Supports both Claude Code and gptme as the inner execution backend.
"""

__version__ = "0.1.0"

from .tools.ralph_loop import tool

__all__ = ["tool"]
