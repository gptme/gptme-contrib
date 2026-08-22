#!/usr/bin/env python3
"""
Validator: an intentionally-skipped checkbox must state a reason.

Bob's task files use three interchangeable markdown forms to mean "deliberately
not doing this":

    - [-] Wire GitHub issue indexing (deferred: 91MB cache mutates under you)
    - [ ] ~~Wire GitHub issue indexing~~ (deferred: spec'd separately)
    - [x] ~~Wire GitHub issue indexing~~ (decided against: superseded)

GFM does not render ``- [-]`` as a checkbox, which is why the two strikethrough
forms exist; all three are treated identically by the parser in
``metaproductivity.tasks``.

The whole point of the marker is to make a deliberate drop *legible*. A bare
``- [-] thing`` with no reason is exactly the silent partial closure the feature
exists to eliminate — it is indistinguishable from "forgot to check the box",
which is how 202 done tasks quietly shipped with unchecked criteria. So: skipped
without a reason is a commit-time error.

A reason is any of:
  * trailing prose after a leading strikethrough span — ``~~thing~~ (deferred: X)``
  * a trailing parenthetical — ``thing (deferred: X)``
  * a dash-separated trailing clause — ``thing — deferred: X``

The reason may wrap onto the following line.

Usage:
    python3 validate_skipped_checkbox_reasons.py tasks/*.md
    python3 validate_skipped_checkbox_reasons.py --strict tasks/*.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "packages" / "metaproductivity" / "src"))

from metaproductivity.tasks import iter_checkbox_items  # noqa: E402

DOC_REFERENCE = "TASKS.md §Best Practices #24 — intentionally-skipped checkboxes"


def check_file(filepath: Path) -> list[tuple[int, str]]:
    """Return [(line_number, offending_line_text)] for reasonless skipped boxes."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except OSError:
        return []

    lines = content.splitlines()
    violations: list[tuple[int, str]] = []
    # Fenced blocks demonstrating the syntax are examples, not real criteria.
    for line_no, kind, _item_text, reason in iter_checkbox_items(
        content, skip_code_blocks=True
    ):
        if kind == "skipped" and not reason:
            violations.append((line_no, lines[line_no - 1].strip()))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Require a stated reason on every intentionally-skipped checkbox",
        epilog=DOC_REFERENCE,
    )
    parser.add_argument("files", nargs="*", type=Path, help="Markdown files to check")
    parser.add_argument(
        "--strict", action="store_true", help="Exit 1 on violations (default: warn)"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    all_violations: list[tuple[Path, int, str]] = []
    for filepath in args.files:
        if filepath.suffix != ".md" or not filepath.exists():
            continue
        for line_no, text in check_file(filepath):
            all_violations.append((filepath, line_no, text))

    if all_violations:
        level = "ERROR" if args.strict else "WARNING"
        print(f"\n{level}: skipped checkbox with no stated reason")
        print(f"  Rule: {DOC_REFERENCE}")
        print("  A skipped box that says nothing is a silent drop — the exact")
        print("  failure mode the marker exists to eliminate.\n")
        print("  BAD:   - [-] Wire GitHub issue indexing")
        print("  GOOD:  - [-] Wire GitHub issue indexing (deferred: spec'd separately)")
        print(
            "  GOOD:  - [ ] ~~Wire GitHub issue indexing~~ (deferred: spec'd separately)"
        )
        print(
            "  GOOD:  - [x] ~~Wire GitHub issue indexing~~ (decided against: superseded)\n"
        )

        for filepath, line_no, text in all_violations:
            print(f"  {filepath}:{line_no}")
            print(f"    {text}")

        distinct_files = len({v[0] for v in all_violations})
        print(
            f"\nFound {len(all_violations)} violation(s) across {distinct_files} file(s)."
        )

        if args.strict:
            return 1

    elif args.verbose:
        print(
            f"✓ All skipped checkboxes state a reason ({len(args.files)} files checked)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
