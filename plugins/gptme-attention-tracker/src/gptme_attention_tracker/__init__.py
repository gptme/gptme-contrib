"""
Attention tracking plugin for gptme.

Combines attention history tracking with attention-based context routing.
Provides:
- Queryable record of what was in context during each session (history)
- HOT/WARM/COLD tiered context management with decay dynamics (router)

These two components work together for meta-learning and context optimization.
"""

__version__ = "0.1.0"
