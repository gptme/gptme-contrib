#!/usr/bin/env python3
"""Pre-commit hook to enforce the allowed set of top-level files and directories.

Adding new top-level entries signals a structural decision. This check ensures
that decision is deliberate: update ALLOWED_ROOT_ENTRIES when adding a new
top-level dir or file, and explain the choice in the PR.

Implementation note: uses ``git ls-files --cached`` (reads the git index) rather
than walking the filesystem and calling ``git check-ignore`` per entry. This is
simpler (one subprocess), correct-by-construction (only committed files matter),
and immune to gitignored on-disk clutter that ``iterdir()`` would surface.

Usage:
    python3 scripts/precommit/check_root_structure.py

As pre-commit hook (.pre-commit-config.yaml):
    - repo: local
      hooks:
      - id: check-root-structure
        name: Check root directory structure
        entry: python3 scripts/precommit/check_root_structure.py
        language: system
        pass_filenames: false
        always_run: true
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def get_tracked_root_entries() -> set[str]:
    """Return first path components of all files tracked in the git index."""
    result = subprocess.run(
        ["git", "ls-files", "--cached"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    entries: set[str] = set()
    for line in result.stdout.splitlines():
        if line:
            entries.add(line.split("/")[0])
    return entries


# Allowed top-level entries. Update this list (with justification in the PR)
# when intentionally adding a new top-level file or directory.
ALLOWED_ROOT_ENTRIES = frozenset(
    [
        # Files
        ".gitattributes",
        ".gitignore",
        ".gitmodules",
        ".jscpd.json",
        ".mailmap",
        ".pre-commit-config.yaml",
        "CONTRIBUTING.md",
        "LICENSE",
        "Makefile",
        "mypy.ini",
        "pyproject.toml",
        "README.md",
        "uv.lock",
        # Directories (trailing slash for readability — checked by name)
        ".github",
        "docs",
        "dotfiles",
        "lessons",
        "packages",
        "plugins",
        "scripts",
        "skills",
        "tests",
    ]
)


def main() -> int:
    entries = get_tracked_root_entries()
    unexpected = entries - ALLOWED_ROOT_ENTRIES
    if not unexpected:
        return 0

    print("check-root-structure: unexpected top-level entries found:")
    for name in sorted(unexpected):
        print(f"  {name}")
    print()
    print(
        "If this is intentional, add the entry to ALLOWED_ROOT_ENTRIES in\n"
        "scripts/precommit/check_root_structure.py and explain the choice in your PR."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
