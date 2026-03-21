"""
YouTube plugin for gptme.

Fetch and summarize YouTube video transcripts.
Moved from gptme core (tools/youtube.py) to gptme-contrib plugin.
"""

__version__ = "0.1.0"

from .tools.youtube import tool

__all__ = ["tool"]
