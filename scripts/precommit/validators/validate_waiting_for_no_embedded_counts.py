#!/usr/bin/env python3
"""
Validator: No embedded dynamic counts in waiting_for fields.

Enforces TASKS.md rule #2: "No dynamic info — Never include '11/19 complete'
or '(at 9 as of 2026-06-20)' in task files."

Specifically catches patterns like:
  - "PR queue below 5 (at 9 as of 2026-06-20)"  → just say "PR queue below 5"
  - "total 8 as of 2026-06-19"                  → remove the count
  - "currently 7 open"                            → remove the count
  - "(9 open, need <5)"                          → "(need <5)"

These patterns create per-cycle churn: every time the count changes, another
session needs to update 20+ task files. The policy threshold belongs; the
snapshot count does not.

Usage:
    python3 validate_waiting_for_no_embedded_counts.py tasks/*.md
    python3 validate_waiting_for_no_embedded_counts.py --strict tasks/*.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Tuple

LESSON_PATH = "lessons/workflow/autonomous-run.md"
LESSON_NAME = "No Embedded Dynamic Counts in waiting_for"

# Same patterns as _STALE_QUEUE_COUNT_PATTERNS in self-review.py, plus variants.
# These detect an embedded numeric snapshot in a waiting_for field.
EMBEDDED_COUNT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bat\s+\d+\s+as\s+of\b", re.IGNORECASE),  # "at 9 as of ..."
    re.compile(r"\btotal\s+\d+\s+as\s+of\b", re.IGNORECASE),  # "total 8 as of ..."
    re.compile(r"\d+\s+open\s+as\s+of\b", re.IGNORECASE),  # "9 open as of ..."
    re.compile(r"\(\s*\d+\s+open[,\)]", re.IGNORECASE),  # "(9 open," or "(9 open)"
    re.compile(r"\bcurrently\s+\d+\b", re.IGNORECASE),  # "currently 7"
    re.compile(r"\bqueue\s+at\s+\d+\b", re.IGNORECASE),  # "queue at 9"
    re.compile(r"\(\s*at\s+\d+\)", re.IGNORECASE),  # "(at 9)"
]

FRONTMATTER_RE = re.compile(r"(?s)^---\s*\n(.*?)\n---\s*\n", re.MULTILINE)
WAITING_FOR_RE = re.compile(
    r"^waiting_for\s*:\s*(.+?)(?=\n\S|\Z)", re.MULTILINE | re.DOTALL
)


def extract_waiting_for(content: str) -> str | None:
    """Extract waiting_for value from YAML frontmatter."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None
    frontmatter_text = m.group(1)
    wf = WAITING_FOR_RE.search(frontmatter_text)
    if not wf:
        return None
    # Collapse multi-line YAML scalar
    raw = wf.group(1).strip()
    # Remove leading '>' or '|' block scalar indicators
    if raw.startswith(("'", '"')):
        raw = raw.strip("'\"")
    return raw


def check_file(filepath: Path) -> list[Tuple[str, str]]:
    """Return list of (pattern_description, snippet) violations."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception:
        return []

    waiting_for = extract_waiting_for(content)
    if not waiting_for:
        return []

    violations: list[Tuple[str, str]] = []
    for pat in EMBEDDED_COUNT_PATTERNS:
        m = pat.search(waiting_for)
        if m:
            start = max(0, m.start() - 20)
            end = min(len(waiting_for), m.end() + 20)
            snippet = "…" + waiting_for[start:end].replace("\n", " ") + "…"
            violations.append((pat.pattern, snippet))

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Block embedded dynamic counts in waiting_for task fields",
        epilog="TASKS.md rule #2: no snapshot counts in durable task fields.",
    )
    parser.add_argument("files", nargs="*", type=Path, help="Task files to check")
    parser.add_argument(
        "--strict", action="store_true", help="Exit 1 on violations (default: warn)"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    all_violations: list[Tuple[Path, str, str]] = []

    for filepath in args.files:
        if not filepath.exists():
            continue
        if not str(filepath).endswith(".md"):
            continue
        # Only check task files
        parts = filepath.parts
        if "tasks" not in parts and not any("task" in p.lower() for p in parts):
            continue

        violations = check_file(filepath)
        for pat_desc, snippet in violations:
            all_violations.append((filepath, pat_desc, snippet))

    if all_violations:
        level = "ERROR" if args.strict else "WARNING"
        print(f"\n{level}: {LESSON_NAME}")
        print("  Rule: TASKS.md §Best Practices #2 — No dynamic info in task files")
        print("  Fix: Remove the count; keep the threshold.\n")
        print("  BAD:  waiting_for: PR queue below 5 (at 9 as of 2026-06-20)")
        print("  GOOD: waiting_for: PR queue below 5\n")

        for filepath, _pat, snippet in all_violations:
            print(f"  {filepath}")
            print(f"    embedded count: {snippet.strip()}")

        print(
            f"\nFound {len(all_violations)} violation(s) across {len({v[0] for v in all_violations})} file(s)."
        )

        if args.strict:
            return 1

    elif args.verbose:
        print(
            f"✓ No embedded counts found in waiting_for fields ({len(args.files)} files checked)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
