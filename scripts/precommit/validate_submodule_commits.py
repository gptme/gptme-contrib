#!/usr/bin/env python3
"""
Pre-commit hook to validate that submodule commits exist in their upstream repositories.

Prevents CI failures caused by submodules pointing to commits that don't exist upstream,
which can happen when:
- Local commits in submodule haven't been pushed
- Commits were force-pushed away in upstream
- Submodule was updated to a branch that was later deleted

Usage:
    python validate_submodule_commits.py [files...]

Exit codes:
    0 - All submodule commits exist upstream
    1 - One or more submodule commits don't exist upstream
"""

import configparser
import subprocess
import sys
from pathlib import Path


def get_submodules() -> dict[str, str]:
    """Parse .gitmodules to get submodule paths and URLs."""
    gitmodules_path = Path(".gitmodules")
    if not gitmodules_path.exists():
        return {}

    config = configparser.ConfigParser()
    config.read(gitmodules_path)

    submodules = {}
    for section in config.sections():
        if section.startswith("submodule "):
            path = config.get(section, "path", fallback=None)
            url = config.get(section, "url", fallback=None)
            if path and url:
                submodules[path] = url

    return submodules


def get_staged_submodule_sha(submodule_path: str) -> str | None:
    """Get the SHA that a submodule is staged/committed to point to."""
    # First check staged changes (takes precedence over HEAD)
    try:
        result = subprocess.run(
            ["git", "diff-index", "--cached", "HEAD", "--", submodule_path],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.strip().split("\n"):
            if line:
                # Format: :old_mode new_mode old_sha new_sha status\tpath
                parts = line.split()
                if len(parts) >= 4:
                    new_sha = parts[3]
                    if new_sha != "0000000000000000000000000000000000000000":
                        return new_sha
    except subprocess.CalledProcessError:
        pass

    # Fall back to HEAD if nothing is staged
    try:
        result = subprocess.run(
            ["git", "ls-tree", "HEAD", submodule_path],
            capture_output=True,
            text=True,
            check=True,
        )
        # Format: mode type sha\tpath
        parts = result.stdout.strip().split()
        if len(parts) >= 3 and parts[1] == "commit":
            return parts[2]
    except subprocess.CalledProcessError:
        pass

    return None


# TODO: The 'url' parameter is not used in check_commit_exists_upstream. Either use it to fetch from the correct remote or remove it to avoid confusion.
def check_commit_exists_upstream(submodule_path: str, sha: str, url: str) -> bool:
    """Check if a commit SHA exists in the upstream repository."""
    # Use git fetch with depth=1 to check if commit is fetchable
    # Note: git ls-remote only finds refs (branches/tags), not arbitrary commits,
    # so we go directly to fetch which works for any commit SHA
    try:
        result = subprocess.run(
            ["git", "-C", submodule_path, "fetch", "--depth=1", "origin", sha],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass

    return False


def main() -> int:
    """Main entry point."""
    submodules = get_submodules()
    if not submodules:
        # No submodules, nothing to check
        return 0

    errors = []
    for path, url in submodules.items():
        sha = get_staged_submodule_sha(path)
        if not sha:
            # Submodule not staged/modified, skip
            continue

        print(f"Checking submodule {path} @ {sha[:12]}...")
        if not check_commit_exists_upstream(path, sha, url):
            errors.append(
                f"Submodule '{path}' points to commit {sha[:12]} "
                f"which doesn't exist in upstream ({url})"
            )

    if errors:
        print("\n❌ Submodule validation failed:\n")
        for error in errors:
            print(f"  - {error}")
        print("\nTo fix: Push your submodule commits to the upstream repository first,")
        print("then update the parent repository's submodule reference.")
        return 1

    if submodules:
        print("✓ All submodule commits exist upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
