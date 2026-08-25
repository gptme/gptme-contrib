"""gptme-browser-semantic — semantic observe/act/extract primitives (Path A).

Public API re-exported for convenience. See ``browser_semantic`` for the
implementation and the design notes in its module docstring.
"""

from .browser_semantic import (
    ActResult,
    ExtractResult,
    ObserveResult,
    browser_act,
    browser_extract,
    browser_observe,
    examples,
    has_semantic_browser,
)

__all__ = [
    "ActResult",
    "ExtractResult",
    "ObserveResult",
    "browser_act",
    "browser_extract",
    "browser_observe",
    "examples",
    "has_semantic_browser",
]
