#!/usr/bin/env python3
"""Parse tagged fields from an autoresearch self-diagnosis log."""

from __future__ import annotations

import re
import sys
from pathlib import Path

FIELDS = ("CAUSE", "BUG_DETAIL", "NEXT_FOCUS")
DEFAULTS = {
    "CAUSE": "unknown",
    "BUG_DETAIL": "NONE",
    "NEXT_FOCUS": "",
}
LINE_RE = re.compile(r"^(CAUSE|BUG_DETAIL|NEXT_FOCUS):\s*(.*)$")


def parse_fields(text: str) -> dict[str, str]:
    """Return the last tagged value for each diagnosis field."""
    parsed = DEFAULTS.copy()
    for raw_line in text.splitlines():
        match = LINE_RE.match(raw_line.strip())
        if match is None:
            continue
        field, value = match.groups()
        parsed[field] = value.strip()
    return parsed


def main(argv: list[str]) -> int:
    if len(argv) not in {2, 3}:
        print(
            f"usage: {Path(argv[0]).name} <diagnosis_log> [CAUSE|BUG_DETAIL|NEXT_FOCUS]",
            file=sys.stderr,
        )
        return 2

    log_path = Path(argv[1])
    text = log_path.read_text(errors="replace") if log_path.exists() else ""
    parsed = parse_fields(text)

    if len(argv) == 3:
        field = argv[2].upper()
        if field not in FIELDS:
            print(f"unknown field: {argv[2]}", file=sys.stderr)
            return 2
        print(parsed[field])
        return 0

    for field in FIELDS:
        print(parsed[field])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
