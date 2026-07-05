"""
Attention tracking tools for gptme.

Provides two complementary tools:
- attention_history: Track and query what files were in context during sessions
- attention_router: Manage dynamic context loading with HOT/WARM/COLD tiers
"""

from .attention_history import tool as attention_history_tool
from .attention_router import tool as attention_router_tool

__all__ = ["attention_history_tool", "attention_router_tool"]
