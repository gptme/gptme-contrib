"""
gptodo plugin for gptme.

Provides delegation tools for coordinator-only agent mode.
Enables the "autonomous-team" pattern where the top-level agent
delegates all work through subagents via gptodo.

Usage:
    gptme --tools gptodo,save "coordinate work on project X"
"""

__version__ = "0.1.0"

from .tools import tool

__all__ = ["tool"]
