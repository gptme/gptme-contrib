#!/usr/bin/env python3
"""Claude Code Stop hook — async memory extraction entry point.

Reads the CC_TRAJECTORY_FILE environment variable and runs the extractor.
Configure in ``.claude/settings.local.json``:

.. code-block:: json

    {
      "hooks": {
        "Stop": "gptme-cc-memory-stop-hook"
      }
    }

The hook must be on PATH or referenced by absolute path. Installing this
package via pip places ``gptme-cc-memory-stop-hook`` on PATH automatically.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Run the memory extractor if CC_TRAJECTORY_FILE is set."""
    if not os.environ.get("CC_TRAJECTORY_FILE"):
        sys.exit(0)  # graceful degradation — no trajectory available

    from gptme_cc_memory.extractor import main as extract_main

    extract_main()


if __name__ == "__main__":
    main()
