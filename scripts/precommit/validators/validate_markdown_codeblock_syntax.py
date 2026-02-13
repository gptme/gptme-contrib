#!/usr/bin/env python3
"""
Validator for: Markdown Codeblock Syntax
Lesson: lessons/tools/markdown-codeblock-syntax.md

Ensures markdown codeblocks always have language tags to prevent parsing cut-offs.

Usage:
    python3 validate_markdown_codeblock_syntax.py file1.md file2.md ...
    python3 validate_markdown_codeblock_syntax.py --strict file1.md file2.md ...
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

# ==============================================================================
# CONFIGURATION
# ==============================================================================

LESSON_PATH = "lessons/tools/markdown-codeblock-syntax.md"
LESSON_NAME = "Markdown Codeblock Syntax"
FILE_PATTERNS = [".md"]

# No predefined language tags - accept any non-empty tag


# ==============================================================================
# VALIDATOR CLASS
# ==============================================================================


class MarkdownCodeblockValidator:
    """Validates markdown codeblocks have language tags."""

    def __init__(self, strict: bool = False, verbose: bool = False):
        self.strict = strict
        self.verbose = verbose
        self.violations: List[Tuple[Path, int, str]] = []

    def should_check_file(self, filepath: Path) -> bool:
        """Check if file should be validated."""
        # Skip lesson files - they need to show anti-patterns
        if "lessons/" in str(filepath):
            return False
        return filepath.suffix in FILE_PATTERNS

    def validate_file(self, filepath: Path) -> bool:
        """Validate a single markdown file."""
        try:
            content = filepath.read_text()
        except Exception as e:
            if self.verbose:
                print(f"Error reading {filepath}: {e}")
            return True  # Don't fail on read errors

        lines = content.split("\n")
        violations_found = False
        fence_count = 0

        for line_num, line in enumerate(lines, start=1):
            # Check for codeblock fence
            if line.strip().startswith("```"):
                fence_count += 1
                # Extract language tag (everything after ```)
                fence_match = re.match(r"^```(\S*)", line.strip())
                if fence_match:
                    lang_tag = fence_match.group(1)

                    # Empty language tag is a violation
                    if not lang_tag:
                        self.violations.append(
                            (filepath, line_num, "No language tag specified")
                        )
                        violations_found = True

        # Check for unclosed code blocks (odd number of fences)
        if fence_count % 2 != 0:
            self.violations.append(
                (
                    filepath,
                    0,
                    f"Unclosed code block detected ({fence_count} fences, expected even number)",
                )
            )
            violations_found = True

        return not violations_found

    def run(self, files: List[Path]) -> int:
        """Run validator on list of files."""
        if self.verbose:
            print(f"Validating {len(files)} markdown files for codeblock syntax")

        for filepath in files:
            if not filepath.exists():
                if self.verbose:
                    print(f"Skipping {filepath}: file does not exist")
                continue

            if not self.should_check_file(filepath):
                if self.verbose:
                    print(f"Skipping {filepath}: not a markdown file")
                continue

            self.validate_file(filepath)

        # Report violations
        if self.violations:
            level = "ERROR" if self.strict else "WARNING"
            print(f"\n{level}: {LESSON_NAME} validation failed")
            print(f"Lesson: {LESSON_PATH}\n")

            for filepath, line_num, reason in self.violations:
                print(f"  {filepath}:{line_num}")
                print(f"    {reason}")
                print(
                    "    Fix: Add language tag like ```txt, ```csv, ```python, etc.\n"
                )

            print(f"Found {len(self.violations)} violation(s)")
            print("\nCodeblocks without language tags can cause:")
            print("  - Content cut-offs during save/append operations")
            print("  - Parser misinterpreting closing ```")
            print("  - Data loss requiring recovery attempts")

            if not self.strict:
                print("\nThese are warnings. Use --strict to fail on violations.")

            return 1 if self.strict else 0

        if self.verbose:
            print("✓ All markdown files have proper codeblock syntax")

        return 0


# ==============================================================================
# CLI INTERFACE
# ==============================================================================


def main():
    """Main entry point for validator."""
    parser = argparse.ArgumentParser(
        description="Validate markdown codeblock syntax",
        epilog=f"Lesson: {LESSON_PATH}",
    )
    parser.add_argument(
        "files", nargs="+", type=Path, help="Markdown files to validate"
    )
    parser.add_argument(
        "--strict", action="store_true", help="Fail with error exit code on violations"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print detailed information"
    )

    args = parser.parse_args()

    validator = MarkdownCodeblockValidator(strict=args.strict, verbose=args.verbose)

    exit_code = validator.run(args.files)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
