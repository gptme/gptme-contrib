"""Read-time tool-output trimmer plugin for gptme."""

from gptme.plugins.plugin import GptmePlugin


def _register_hooks() -> None:
    from .hooks import register

    register()


plugin = GptmePlugin(
    name="tooloutput_trimmer",
    register_hooks=_register_hooks,
)

__all__ = ["plugin"]
