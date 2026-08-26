#!/usr/bin/env python3
"""Enforce allowed set of top-level files and directories via YAML config file.

This validator reads an allowlist from a YAML config file (default:
`root-structure-allowlist.yaml` in the repo root) and rejects any tracked
top-level entries not on that list. The allowlist approach scales across
multiple repos without code duplication.

Each repo using this validator owns a `root-structure-allowlist.yaml` file
listing its permitted top-level entries, one per line.

Usage (from repo root):
    python3 gptme-contrib/scripts/precommit/validators/validate_root_structure_yaml.py

Or with a custom config path:
    python3 gptme-contrib/scripts/precommit/validators/validate_root_structure_yaml.py \\
      --config my-root-allowlist.yaml

As a pre-commit hook in .pre-commit-config.yaml (language and dependencies
are declared in .pre-commit-hooks.yaml and do not need repeating here):
    - repo: https://github.com/gptme/gptme-contrib
      rev: <SHA or tag>
      hooks:
      - id: validate-root-structure

Config file format (YAML, one entry per line):
    ---
    allowed_entries:
      - .github
      - .gitignore
      - README.md
      - root-structure-allowlist.yaml  # the config file itself must be listed
      - src
      - tests
"""

import argparse
import subprocess
import sys
from pathlib import Path

import yaml


def get_repo_root() -> Path:
    """Return the repository root via git rev-parse."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
    )
    return Path(result.stdout.rstrip(b"\r\n").decode("utf-8", errors="surrogateescape"))


def get_tracked_root_entries(repo_root: Path) -> set[str]:
    """Return first path components of all files in the git index."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    entries: set[str] = set()
    for path in result.stdout.split(b"\0"):
        if path:
            entries.add(
                path.split(b"/", maxsplit=1)[0].decode(
                    "utf-8", errors="surrogateescape"
                )
            )
    return entries


def load_allowlist(config_path: Path) -> "frozenset[str] | None":
    """Load allowed entries from YAML config file.

    Expects a file with structure:
        ---
        allowed_entries:
          - entry1
          - entry2

    Returns the allowed entries, or None if the config could not be loaded.
    An empty-but-valid ``allowed_entries`` list returns an empty frozenset,
    which is distinct from a load failure.
    """
    if not config_path.exists():
        print(
            f"validate-root-structure: config file not found: {config_path}",
            file=sys.stderr,
        )
        return None

    try:
        with open(config_path, encoding="utf-8", errors="surrogateescape") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict) or "allowed_entries" not in data:
            print(
                f"validate-root-structure: config file missing 'allowed_entries' key: {config_path}",
                file=sys.stderr,
            )
            return None

        entries = data["allowed_entries"]
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            print(
                f"validate-root-structure: 'allowed_entries' must be a list: {config_path}",
                file=sys.stderr,
            )
            return None

        return frozenset(str(entry) for entry in entries)
    except yaml.YAMLError as e:
        print(
            f"validate-root-structure: failed to parse YAML config: {e}",
            file=sys.stderr,
        )
        return None
    except OSError as e:
        print(
            f"validate-root-structure: error reading config: {e}",
            file=sys.stderr,
        )
        return None


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=str,
        default="root-structure-allowlist.yaml",
        help="Path to YAML allowlist file (default: root-structure-allowlist.yaml in repo root)",
    )
    args = parser.parse_args(argv)

    try:
        repo_root = get_repo_root()
    except (subprocess.CalledProcessError, OSError) as error:
        print(
            f"validate-root-structure: unable to find git repo root: {error}",
            file=sys.stderr,
        )
        return 1

    # Resolve config path relative to repo root if not absolute
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root / config_path

    allowed = load_allowlist(config_path)
    if allowed is None:
        # load_allowlist already explained what went wrong.
        if not config_path.exists():
            print(
                f"If this is intentional, create {config_path} with allowed entries.",
                file=sys.stderr,
            )
        return 1

    try:
        entries = get_tracked_root_entries(repo_root)
    except (subprocess.CalledProcessError, OSError) as error:
        print(
            f"validate-root-structure: git ls-files failed: {error}",
            file=sys.stderr,
        )
        return 1

    unexpected = entries - allowed
    if not unexpected:
        return 0

    print("validate-root-structure: unexpected top-level entries found:")
    for name in sorted(unexpected):
        print(f"  {name}")
    print()
    print(
        f"If this is intentional, add the entry to {config_path} and explain\n"
        "the choice in your commit message."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
