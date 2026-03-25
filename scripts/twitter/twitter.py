#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "tweepy>=4.14.0",
#   "rich>=13.0.0",
#   "python-dotenv>=1.0.0",
#   "click>=8.0.0",
#   "flask>=3.0.0",
#   "gptmail[oauth] @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=packages/gptmail",
# ]
# [tool.uv]
# exclude-newer = "2024-01-01T00:00:00Z"
# ///
"""Backward-compatible wrapper for the packaged gptwitter CLI."""

import sys
from pathlib import Path

PACKAGE_SRC = Path(__file__).resolve().parents[2] / "packages" / "gptwitter" / "src"
sys.path.insert(0, str(PACKAGE_SRC))


def main() -> None:
    from gptwitter.cli import main as package_main  # type: ignore[import-not-found]

    package_main()


if __name__ == "__main__":
    main()
