"""Read-time tool-output trimmer plugin for gptme."""

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
    from .hooks import register_summarizer, register_trimmer

    register_trimmer()
    register_summarizer()


plugin = GptmePlugin(
    name="tooloutput_trimmer",
    register_hooks=_register_hooks,
)

__all__ = ["plugin"]
