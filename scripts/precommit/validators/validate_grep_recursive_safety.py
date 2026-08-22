#!/usr/bin/env python3
"""
Validator for: Grep Recursive Safety
Lesson: lessons/tools/grep-recursive-safety.md

Prevents dangerous recursive grep operations that can create feedback loops
and crash VMs by matching their own output in log files.

Real incident (2025-11-11):
- Action: `grep -r "pattern" .` in directory with logs/
- Result: grep matched its own output, exponential log growth (6.4GB)
- Impact: VM crashed, required manual recovery

Usage:
    python3 validate_grep_recursive_safety.py file1.md file2.sh ...
    python3 validate_grep_recursive_safety.py --strict file1.md file2.sh ...
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

# ==============================================================================
# CONFIGURATION
# ==============================================================================

LESSON_PATH = "lessons/tools/grep-recursive-safety.md"
LESSON_NAME = "Grep Recursive Safety"
FILE_PATTERNS = [".md", ".sh", ".bash", ".py"]

# Patterns that indicate dangerous recursive grep
DANGEROUS_PATTERNS = [
    # Basic recursive grep without exclusions
    r"grep\s+-[a-zA-Z]*r[a-zA-Z]*\s+['\"]?[^'\"]+['\"]?\s+\.",
    r"grep\s+-[a-zA-Z]*R[a-zA-Z]*\s+['\"]?[^'\"]+['\"]?\s+\.",
    # Recursive grep in current directory variations
    r"grep\s+-[a-zA-Z]*r[a-zA-Z]*\s+['\"]?[^'\"]+['\"]?\s+\./",
    r"grep\s+-[a-zA-Z]*R[a-zA-Z]*\s+['\"]?[^'\"]+['\"]?\s+\./",
    # Without explicit directory (defaults to current)
    r"grep\s+-[a-zA-Z]*r[a-zA-Z]*\s+['\"]?[^'\"]+['\"]?\s*$",
    r"grep\s+-[a-zA-Z]*R[a-zA-Z]*\s+['\"]?[^'\"]+['\"]?\s*$",
]

# Safe patterns that should be allowed
SAFE_PATTERNS = [
    # Has --exclude-dir for logs
    r"grep.*--exclude-dir[=\s]logs",
    r"grep.*--exclude-dir[=\s]['\"]logs['\"]",
    # Has --exclude-dir for common log directories
    r"grep.*--exclude-dir[=\s]\.git",
    r"grep.*--exclude-dir[=\s]node_modules",
    # Uses git grep instead (safer)
    r"git\s+grep",
    # Targets specific directory (not current)
    r"grep\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+['\"]?[^'\"]+['\"]?\s+[a-zA-Z_/]+",
    # Inside comments
    r"^\s*#.*grep",
    # Inside quotes (documentation)
    r"['\"].*grep.*-[a-zA-Z]*[rR]",
    # In examples or documentation blocks
    r"```.*grep.*-[a-zA-Z]*[rR]",
]

# Suggested alternatives
SUGGESTED_ALTERNATIVES = [
    "Use 'git grep' instead (only searches tracked files)",
    "Use 'grep -r --exclude-dir=logs' to skip log directories",
    "Use 'grep -r PATTERN specific/directory' to target specific paths",
    "Use 'rg' (ripgrep) which excludes .gitignore patterns by default",
]


# ==============================================================================
# VALIDATOR CLASS
# ==============================================================================


class GrepRecursiveSafetyValidator:
    """Validates that recursive grep operations are safe."""

    def __init__(self, strict: bool = False, verbose: bool = False):
        self.strict = strict
        self.verbose = verbose
        self.violations: List[Tuple[Path, int, str, str]] = []

    def should_check_file(self, filepath: Path) -> bool:
        """Check if file should be validated."""
        # Skip lesson files - they need to show anti-patterns
        if "lessons/" in str(filepath):
            return False
        # Skip test files that might contain anti-patterns as examples
        if "tests/" in str(filepath) or "test_" in filepath.name:
            return False
        # Skip documentation examples
        if "examples/" in str(filepath) or "templates/" in str(filepath):
            return False
        return filepath.suffix in FILE_PATTERNS

    def is_safe_pattern(self, line: str) -> bool:
        """Check if line contains a safe pattern."""
        for pattern in SAFE_PATTERNS:
            if re.search(pattern, line):
                return True
        return False

    def is_dangerous_pattern(self, line: str) -> bool:
        """Check if line contains a dangerous pattern."""
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, line):
                return True
        return False

    def check_markdown_context(self, lines: List[str], line_num: int) -> bool:
        """Check if line is in markdown code block context."""
        # Look backward for fence markers
        in_code_block = False
        for i in range(line_num - 1, max(0, line_num - 50), -1):
            line = lines[i].strip()
            if line.startswith("```"):
                in_code_block = not in_code_block
        return in_code_block

    def validate_file(self, filepath: Path) -> bool:
        """Validate a single file for grep recursive safety."""
        if not self.should_check_file(filepath):
            if self.verbose:
                print(f"Skipping {filepath}")
            return True

        try:
            with open(filepath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"Error reading {filepath}: {e}", file=sys.stderr)
            return False

        file_valid = True
        for line_num, line in enumerate(lines, start=1):
            # Skip if safe pattern detected
            if self.is_safe_pattern(line):
                continue

            # Check for dangerous pattern
            if self.is_dangerous_pattern(line):
                # For markdown files, check if in code block
                if filepath.suffix == ".md":
                    # Only check shell code blocks
                    in_code = self.check_markdown_context(lines, line_num)
                    if not in_code:
                        continue

                # Found violation
                self.violations.append(
                    (filepath, line_num, line.strip(), "Dangerous recursive grep")
                )
                file_valid = False

        return file_valid

    def validate_files(self, filepaths: List[Path]) -> bool:
        """Validate multiple files."""
        all_valid = True
        for filepath in filepaths:
            if not self.validate_file(filepath):
                all_valid = False

        return all_valid

    def print_violations(self) -> None:
        """Print all violations found."""
        if not self.violations:
            return

        print(f"\n❌ {LESSON_NAME} violations found:\n", file=sys.stderr)

        for filepath, line_num, line, reason in self.violations:
            print(f"  {filepath}:{line_num}", file=sys.stderr)
            print(f"    {reason}", file=sys.stderr)
            print(f"    > {line}", file=sys.stderr)
            print(file=sys.stderr)

        print("📖 Safe alternatives:", file=sys.stderr)
        for alt in SUGGESTED_ALTERNATIVES:
            print(f"  • {alt}", file=sys.stderr)
        print(file=sys.stderr)

        print(f"📚 See: {LESSON_PATH}", file=sys.stderr)
        print(file=sys.stderr)


# ==============================================================================
# MAIN
# ==============================================================================


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description=f"Validate {LESSON_NAME}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("files", nargs="+", type=Path, help="Files to check")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Strict mode (fail on any violation)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output",
    )

    args = parser.parse_args()

    validator = GrepRecursiveSafetyValidator(
        strict=args.strict,
        verbose=args.verbose,
    )

    success = validator.validate_files(args.files)
    validator.print_violations()

    if not success:
        if validator.strict:
            return 1  # Fail in strict mode
        return 0  # Warning only in non-strict mode

    return 0


if __name__ == "__main__":
    sys.exit(main())
