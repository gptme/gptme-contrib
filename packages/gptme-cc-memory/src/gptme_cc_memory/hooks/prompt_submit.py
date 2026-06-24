#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook — inject context from cross-session memory.

Reads stdin (JSON with ``session_id``, ``transcript_path``, ``prompt``), scores
memory files for relevance, and outputs the injection block to stdout.
Configured in ``.claude/settings.local.json``:

.. code-block:: json

    {
      "hooks": {
        "UserPromptSubmit": "gptme-cc-memory-prompt-submit"
      }
    }

The hook script must be on PATH or referenced by absolute path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from gptme_cc_memory.injector import inject_memories

# Default workspace: GPTME_CC_MEMORY_DIR env var, or the current working
# directory if not set. The memory directory is expected at
# ``<workspace>/memory/``.
WORKSPACE = Path(os.environ.get("GPTME_CC_MEMORY_DIR", os.getcwd()))
MEMORY_DIR = WORKSPACE / "memory"
STATE_DIR = WORKSPACE / "state" / "cc-memory"
METADATA_FILE = STATE_DIR / "metadata.json"
GUIDANCE_FILE = MEMORY_DIR / "guidance.md"
PENDING_UPDATES_FILE = MEMORY_DIR / "pending-updates.md"
PENDING_ITEMS_FILE = MEMORY_DIR / "pending-items.md"
PENDING_SESSION_CONTEXT_FILE = MEMORY_DIR / "pending-session-context.md"


def main() -> None:
    """Read stdin and inject relevant memories."""
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return

    prompt = input_data.get("prompt", "")
    if not prompt:
        return

    if not MEMORY_DIR.is_dir():
        return

    result = inject_memories(
        prompt,
        memory_dir=MEMORY_DIR,
        metadata_file=METADATA_FILE,
        guidance_file=GUIDANCE_FILE,
        pending_updates_file=PENDING_UPDATES_FILE,
        pending_items_file=PENDING_ITEMS_FILE,
        pending_session_context_file=PENDING_SESSION_CONTEXT_FILE,
    )

    if result:
        print(result)


if __name__ == "__main__":
    main()
