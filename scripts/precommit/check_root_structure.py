#!/usr/bin/env python3
"""
Pre-commit hook: enforce gptme-contrib root directory structure.

Prevents top-level directory sprawl by rejecting new entries not on the allowlist.
To add a new top-level entry, update ALLOWED_ROOT_ENTRIES below with justification.
"""

import sys
from pathlib import Path

# Canonical top-level entries allowed in gptme-contrib root.
# Keep this tight — new dirs/files should have a structural reason to exist,
# not just "this is where I dropped it."
ALLOWED_ROOT_ENTRIES = {
    # Git internals (always present, never committed)
    ".git",
    # Config / tooling
    ".github",
    ".gitignore",
    ".jscpd.json",
    ".mailmap",
    ".mypy_cache",
    ".pre-commit-config.yaml",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "mypy.ini",
    "pyproject.toml",
    "uv.lock",
    # Standard repo files
    "CONTRIBUTING.md",
    "LICENSE",
    "Makefile",
    "README.md",
    # Active structural dirs
    "commands",   # gptme command definitions
    "docs",       # protocol specs, plugin docs
    "dotfiles",   # dotfile configs for agents
    "journal",    # session journals
    "lessons",    # lesson system (injected into agent context)
    "packages",   # uv workspace packages
    "plugins",    # gptme plugin definitions
    "schemas",    # JSON Schema files (candidate for consolidation into docs/)
    "scripts",    # utility scripts and pre-commit hooks
    "skills",     # skill bundles (Anthropic skill format)
    "tests",      # test suite
    "tools",      # standalone tools (candidate for consolidation into scripts/)
}


def main() -> int:
    root = Path(__file__).parent.parent.parent
    violations = []

    for entry in sorted(root.iterdir()):
        name = entry.name
        # Skip hidden entries already covered by allowlist; git won't track them
        if name not in ALLOWED_ROOT_ENTRIES:
            violations.append(name)

    if violations:
        print("check-root-structure: unexpected top-level entries:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nAdd an explicit entry to ALLOWED_ROOT_ENTRIES in "
            "scripts/precommit/check_root_structure.py with a comment explaining "
            "why it belongs at the root.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
