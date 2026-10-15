"""gptme_review — self-hosted AI PR reviewer engine.

Lifted from ErikBjare/bob (scripts/github/ai_review_lib.py) into gptme-contrib
so the gate consumers in contrib-lib share one import path with the engine.

Public API: import from gptme_review.core directly, or use this package as
a namespace (gptme_review.lifecycle, gptme_review.core).

Package commit logging: for source/editable installs the checkout SHA is printed
when AI_REVIEW_LOG_PACKAGE_COMMIT=1 is set. Wheel installs report it unavailable;
a surrounding application's repository must not be mistaken for package provenance.
"""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

try:
    __version__ = importlib.metadata.version("gptme-review")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0+unknown"


def _package_checkout(source: Path) -> Path | None:
    """Find source provenance without attributing a wheel to its host repo."""
    for candidate in source.resolve().parents:
        if candidate.name in {"site-packages", "dist-packages"}:
            return None
        if (candidate / ".git").exists():
            return candidate
    return None


_PACKAGE_ROOT = _package_checkout(Path(__file__))

if os.environ.get("AI_REVIEW_LOG_PACKAGE_COMMIT") and _PACKAGE_ROOT is None:
    print(
        "gptme_review package commit: unavailable (no source checkout)", file=sys.stderr
    )
elif os.environ.get("AI_REVIEW_LOG_PACKAGE_COMMIT"):
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
