"""User memories plugin for gptme.

Registers a SESSION_END hook that mines recent personal conversations and
updates ~/.local/share/gptme/user-memories.md with extracted user facts.

The memories file can be included in future sessions via gptme.toml:

    [prompt]
    files = ["~/.local/share/gptme/user-memories.md"]

Or in context.sh:

    cat ~/.local/share/gptme/user-memories.md 2>/dev/null
"""

__version__ = "0.1.0"
