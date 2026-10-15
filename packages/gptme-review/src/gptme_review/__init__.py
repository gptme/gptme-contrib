"""gptme_review — self-hosted AI PR reviewer engine.

Lifted from ErikBjare/bob (scripts/github/ai_review_lib.py) into gptme-contrib
so the gate consumers in contrib-lib share one import path with the engine.

Public API: import from gptme_review.core directly, or use this package as
a namespace (gptme_review.lifecycle, gptme_review.core).

Package commit logging: the resolved commit SHA is printed to stderr when
AI_REVIEW_LOG_PACKAGE_COMMIT=1 is set, so a stale submodule pin is visible.
"""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

__version__ = importlib.metadata.version("gptme-review")

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # site-packages/..

if os.environ.get("AI_REVIEW_LOG_PACKAGE_COMMIT"):
    try:
        _sha = subprocess.check_output(
            ["git", "-C", str(_PACKAGE_ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        print(f"gptme_review package commit: {_sha}", file=sys.stderr)
    except Exception:
        pass

from gptme_review import (  # noqa: E402
    core,  # noqa: F401
    lifecycle,  # noqa: F401
)
