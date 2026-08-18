#!/usr/bin/env python3
"""Pre-commit hook to enforce the allowed set of top-level files and directories.

Adding new top-level entries signals a structural decision. This check ensures
that decision is deliberate: update ALLOWED_ROOT_ENTRIES when adding a new
top-level dir or file, and explain the choice in the PR.

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


def is_gitignored(path: Path) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


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
        "community_plugins.json",
        "CONTRIBUTING.md",
        "LICENSE",
        "Makefile",
        "mypy.ini",
        "pyproject.toml",
        "README.md",
        "uv.lock",
        # Directories (trailing slash for readability — checked by name)
        ".git",
        ".github",
        "commands",
        "docs",
        "dotfiles",
        "lessons",
        "packages",
        "plugins",
        "schemas",
        "scripts",
        "skills",
        "tests",
        "tools",
    ]
)


def main() -> int:
    entries = {p.name for p in REPO_ROOT.iterdir() if not is_gitignored(p)}
    unexpected = entries - ALLOWED_ROOT_ENTRIES
    if not unexpected:
        return 0

    print("check-root-structure: unexpected top-level entries found:")
    for name in sorted(unexpected):
        path = REPO_ROOT / name
        kind = "dir" if path.is_dir() else "file"
        print(f"  {kind}: {name}")
    print()
    print(
        "If this is intentional, add the entry to ALLOWED_ROOT_ENTRIES in\n"
        "scripts/precommit/check_root_structure.py and explain the choice in your PR."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
