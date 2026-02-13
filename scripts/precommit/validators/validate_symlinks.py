#!/usr/bin/env python3
"""
Validator for: Symlink Integrity
Lesson: N/A (general best practice)

Ensures all symlinks in the repository point to valid targets.

Usage:
    python3 validate_symlinks.py
    python3 validate_symlinks.py --verbose
"""

import argparse
import sys
from pathlib import Path
from typing import List, Tuple

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Directories to skip (relative to repo root)
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", "worktree"}


# ==============================================================================
# VALIDATOR CLASS
# ==============================================================================


class SymlinkValidator:
    """Validates all symlinks point to valid targets."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.violations: List[Tuple[Path, str]] = []

    def find_symlinks(self, root: Path) -> List[Path]:
        """Find all symlinks in the repository."""
        symlinks = []
        for path in root.rglob("*"):
            # Skip certain directories
            if any(skip in path.parts for skip in SKIP_DIRS):
                continue
            if path.is_symlink():
                symlinks.append(path)
        return symlinks

    def validate_symlink(self, symlink: Path, root: Path) -> bool:
        """Check if a symlink points to a valid target."""
        try:
            # Get the target path
            target = symlink.resolve()

            # Check if target exists
            if not target.exists():
                # Get the raw link target for error message
                link_target = symlink.readlink()
                self.violations.append(
                    (symlink, f"Broken symlink: points to non-existent '{link_target}'")
                )
                return False

            if self.verbose:
                print(f"✓ {symlink.relative_to(root)} -> {symlink.readlink()}")
            return True

        except OSError as e:
            self.violations.append((symlink, f"Error checking symlink: {e}"))
            return False

    def validate_all(self, root: Path) -> bool:
        """Validate all symlinks in the repository."""
        symlinks = self.find_symlinks(root)

        if self.verbose:
            print(f"Found {len(symlinks)} symlinks to validate")

        all_valid = True
        for symlink in symlinks:
            if not self.validate_symlink(symlink, root):
                all_valid = False

        return all_valid

    def report(self, root: Path) -> None:
        """Report validation results."""
        if not self.violations:
            print(f"✅ All {len(self.find_symlinks(root))} symlinks are valid")
            return

        print(f"❌ Found {len(self.violations)} broken symlink(s):\n")
        for path, message in self.violations:
            rel_path = path.relative_to(root)
            print(f"  {rel_path}: {message}")


# ==============================================================================
# MAIN
# ==============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate all symlinks in the repository"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show all symlinks checked"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect)",
    )
    args = parser.parse_args()

    # Find repository root
    if args.root:
        root = args.root.resolve()
    else:
        # Walk up to find .git directory
        root = Path.cwd()
        while not (root / ".git").exists() and root.parent != root:
            root = root.parent

    if not (root / ".git").exists():
        print("Error: Could not find repository root", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"Validating symlinks in: {root}\n")

    validator = SymlinkValidator(verbose=args.verbose)
    all_valid = validator.validate_all(root)
    validator.report(root)

    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(main())
