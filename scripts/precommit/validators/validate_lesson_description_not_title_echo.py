#!/usr/bin/env python3
"""
Validator for: Lesson description must not echo the title
Lesson: lessons/autonomous/lesson-quality-standards.md

Hybrid keyword+semantic lesson retrieval scores incoming session prompts against
the `description:` frontmatter field. When `description` just restates the lesson
title, it carries no symptom/situation signal beyond the title — those lessons
are under-retrievable by paraphrase matching (the `loo_plateau` failure mode).

Session 248d (2026-05-24) rewrote 94 title-echo descriptions into symptom-based
statements. This guard prevents the defect from re-accumulating: a new or edited
lesson whose `description` equals its `# Title` (case/whitespace/quote-normalized)
fails the check.

Reproduce basis: tasks/lesson-description-title-echo-batch.md "Reproduce the defect list".

Usage:
    python3 validate_lesson_description_not_title_echo.py lessons/foo.md ...
    python3 validate_lesson_description_not_title_echo.py --strict lessons/foo.md ...
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

# ==============================================================================
# CONFIGURATION
# ==============================================================================

LESSON_PATH = "lessons/autonomous/lesson-quality-standards.md"
LESSON_NAME = "Lesson description must not echo the title"

# Path fragments that are not real runtime lessons (mirror the validate-lessons
# hook excludes plus the reproduce snippet's skip list).
SKIP_FRAGMENTS = (
    "/archived/",
    "/proposed/",
    "/templates/",
    "rejected",
)
SKIP_NAMES = {"README.md", "TODO.md"}

TITLE_RE = re.compile(r"^#\s+(.+)$", re.M)
DESC_RE = re.compile(r'^\s*description:\s*"?(.+?)"?\s*$', re.M)


def _normalize(s: str) -> str:
    """Case/whitespace/quote-normalized form for echo comparison."""
    return s.strip().strip('"').strip().lower()


# ==============================================================================
# VALIDATOR CLASS
# ==============================================================================


class LessonDescriptionEchoValidator:
    """Flags lessons whose description frontmatter just restates the title."""

    def __init__(self, strict: bool = False, verbose: bool = False):
        self.strict = strict
        self.verbose = verbose
        self.violations: List[Tuple[Path, str]] = []

    def should_check_file(self, filepath: Path) -> bool:
        """Only check runtime lesson markdown files."""
        sp = str(filepath)
        if "lessons/" not in sp:
            return False
        if filepath.name in SKIP_NAMES:
            return False
        if any(frag in sp for frag in SKIP_FRAGMENTS):
            return False
        return filepath.suffix == ".md"

    @staticmethod
    def extract_title_and_description(
        content: str,
    ) -> Tuple[str | None, str | None]:
        """Return (title, description) from a lesson file, or None for missing."""
        tm = TITLE_RE.search(content)
        dm = DESC_RE.search(content)
        title = tm.group(1) if tm else None
        desc = dm.group(1) if dm else None
        return title, desc

    def validate_file(self, filepath: Path) -> bool:
        """Validate a single lesson file. Returns True if OK."""
        try:
            content = filepath.read_text(errors="ignore")
        except Exception as e:
            if self.verbose:
                print(f"Error reading {filepath}: {e}")
            return True  # Don't fail on read errors

        title, desc = self.extract_title_and_description(content)
        # Missing description is a separate concern (the lesson validator owns
        # required-field checks); only flag a present-but-echoing description.
        if title is None or desc is None:
            return True

        if _normalize(title) == _normalize(desc):
            self.violations.append((filepath, title.strip()))
            return False
        return True

    def run(self, files: List[Path]) -> int:
        """Run validator on list of files."""
        if self.verbose:
            print(f"Checking {len(files)} file(s) for title-echo descriptions")

        for filepath in files:
            if not filepath.exists():
                continue
            if not self.should_check_file(filepath):
                continue
            self.validate_file(filepath)

        if self.violations:
            level = "ERROR" if self.strict else "WARNING"
            print(f"\n{level}: {LESSON_NAME}")
            print(f"Lesson: {LESSON_PATH}\n")
            for filepath, title in self.violations:
                print(f"  {filepath}")
                print(f'    description: just restates the title "{title}"')
                print(
                    "    Fix: rewrite description to state the symptom or situation a real\n"
                    "         session prompt would contain (drawn from Rule/Context/Detection),\n"
                    "         not the title restated.\n"
                )
            print(f"Found {len(self.violations)} title-echo description(s)")
            print(
                "\nWhy this matters: hybrid retrieval scores prompts against the\n"
                "description field; a title-echo gives no symptom signal beyond the\n"
                "title, so the lesson is under-retrievable by paraphrase (loo_plateau)."
            )
            if not self.strict:
                print("\nThese are warnings. Use --strict to fail on violations.")
            return 1 if self.strict else 0

        if self.verbose:
            print("✓ No title-echo descriptions found")
        return 0


# ==============================================================================
# CLI INTERFACE
# ==============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate that lesson descriptions don't just restate the title",
        epilog=f"Lesson: {LESSON_PATH}",
    )
    parser.add_argument("files", nargs="+", type=Path, help="Lesson files to validate")
    parser.add_argument(
        "--strict", action="store_true", help="Fail with error exit code on violations"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print detailed information"
    )
    args = parser.parse_args()

    validator = LessonDescriptionEchoValidator(strict=args.strict, verbose=args.verbose)
    sys.exit(validator.run(args.files))


if __name__ == "__main__":
    main()
