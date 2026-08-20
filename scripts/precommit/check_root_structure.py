#!/usr/bin/env python3
"""Pre-commit hook to enforce the allowed set of top-level files and directories.

Adding new top-level entries signals a structural decision. This check ensures
that decision is deliberate: update ALLOWED_ROOT_ENTRIES (or the ``--allow``
args in ``.pre-commit-config.yaml``) when adding a new top-level dir or file,
and explain the choice in the PR.

Implementation note: uses ``git ls-files --cached`` (reads the git index) rather
than walking the filesystem and calling ``git check-ignore`` per entry. This is
simpler (one subprocess), correct-by-construction (only committed files matter),
and immune to gitignored on-disk clutter that ``iterdir()`` would surface.

Usage (local):
    python3 scripts/precommit/check_root_structure.py

As a local pre-commit hook in the same repo (.pre-commit-config.yaml):
    - repo: local
      hooks:
      - id: check-root-structure
        name: Check root directory structure
        entry: python3 scripts/precommit/check_root_structure.py
        language: system
        pass_filenames: false
        always_run: true

As a shared hook from gptme-contrib (another repo's .pre-commit-config.yaml):
    - repo: https://github.com/gptme/gptme-contrib
      rev: <SHA or tag>
      hooks:
      - id: check-root-structure
        args:
          - --allow=.github
          - --allow=.gitignore
          - --allow=README.md
          - --allow=src
          - --allow=tests
          # ... add every allowed root entry for your repo

The ``--allow`` args replace the built-in gptme-contrib defaults, so the
consuming repo must list every entry it wants to permit.
"""

import argparse
import subprocess
import sys
from pathlib import Path


def get_repo_root() -> Path:
    """Return the repository root, resolved via git.

    This works correctly both when called locally and when executed as a
    remote pre-commit hook (where ``Path(__file__)`` points into a cache
    directory, not the consuming repo).
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def get_tracked_root_entries(repo_root: "Path | None" = None) -> set[str]:
    """Return first path components of all files tracked in the git index.

    Args:
        repo_root: Directory to run ``git ls-files`` from.  If ``None``, uses
            the directory three levels above this script (i.e. the
            gptme-contrib root when called locally), which is the backwards-
            compatible default.  Pass ``get_repo_root()`` to get the correct
            value when used as a remote pre-commit hook.
    """
    cwd = repo_root if repo_root is not None else Path(__file__).parent.parent.parent
    result = subprocess.run(
        ["git", "ls-files", "--cached"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    entries: set[str] = set()
    for line in result.stdout.splitlines():
        if line:
            entries.add(line.split("/")[0])
    return entries


# Default allowlist for gptme-contrib itself.
# Other repos using this as a remote hook should supply ``--allow`` args
# in their ``.pre-commit-config.yaml`` instead of modifying this list.
ALLOWED_ROOT_ENTRIES = frozenset(
    [
        # Files
        ".gitattributes",
        ".gitignore",
        ".gitmodules",
        ".jscpd.json",
        ".mailmap",
        ".pre-commit-config.yaml",
        ".pre-commit-hooks.yaml",
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


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--allow",
        metavar="ENTRY",
        action="append",
        dest="allowed",
        default=[],
        help=(
            "Allow this top-level entry (repeatable). "
            "If not specified, uses the built-in gptme-contrib defaults. "
            "Specify once per permitted entry when using this hook from another repo."
        ),
    )
    args = parser.parse_args(argv)

    # When --allow args are provided, they define the complete allowlist.
    # When absent, fall back to the gptme-contrib defaults.
    allowed = frozenset(args.allowed) if args.allowed else ALLOWED_ROOT_ENTRIES

    entries = get_tracked_root_entries(get_repo_root())
    unexpected = entries - allowed
    if not unexpected:
        return 0

    print("check-root-structure: unexpected top-level entries found:")
    for name in sorted(unexpected):
        print(f"  {name}")
    print()
    if args.allowed:
        print(
            "If this is intentional, add ``--allow=ENTRY`` to the hook's args in\n"
            ".pre-commit-config.yaml and explain the choice in your PR."
        )
    else:
        print(
            "If this is intentional, add the entry to ALLOWED_ROOT_ENTRIES in\n"
            "scripts/precommit/check_root_structure.py and explain the choice in your PR."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
