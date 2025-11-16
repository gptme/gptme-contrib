#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "click>=8.0.0",
#   "gptme @ git+https://github.com/ErikBjare/gptme.git",
# ]
# [tool.uv]
# exclude-newer = "2025-10-02T00:00:00Z"
# ///
"""
Lesson Generator Entrypoint

Thin wrapper that invokes the lessons.generate module.
"""

from lessons.generate import cli

if __name__ == "__main__":
    cli()
