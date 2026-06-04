"""Read-time SmartCrusher compression plugin for gptme.

Runs as a `generation_pre` hook at priority 201 (before the tooloutput-trimmer
at 200). Applies SmartCrusher's lossless `crush()` to structured/tabular tool
outputs before they reach the trimmer. Leaves unstructured outputs in place for
the trimmer to handle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

try:
    from gptme.plugins.plugin import GptmePlugin
except ModuleNotFoundError:

    @dataclass(frozen=True)
    class GptmePlugin:  # pragma: no cover - exercised in old-gptme envs
        """Compatibility shim for pre-unified-plugin gptme releases."""

        name: str
        register_hooks: Callable[[], None] | None = None


def _register_hooks() -> None:
    from .hooks import register

    register()


plugin = GptmePlugin(
    name="headroom_compressor",
    register_hooks=_register_hooks,
)

__all__ = ["plugin"]
