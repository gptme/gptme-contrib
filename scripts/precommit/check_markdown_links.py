#!/usr/bin/env python3
"""Pre-commit hook to verify relative links in markdown files.

Usage:
    python3 scripts/precommit/check_markdown_links.py [files...]

As pre-commit hook (.pre-commit-config.yaml):
    - repo: local
      hooks:
      - id: check-markdown-links
        name: Check markdown links
        entry: python3 scripts/precommit/check_markdown_links.py
        language: system
        types: [markdown]
        pass_filenames: true
"""

import re
import sys
from pathlib import Path

# Link pattern for markdown
LINK_PATTERN = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

# Protocols to skip (URLs)
SKIP_PROTOCOLS = ("http://", "https://", "ftp://", "mailto:", "#")

# Directories to skip
SKIP_DIRS = {"worktree", ".git", "node_modules", "__pycache__", ".venv", "templates"}


def get_repo_root() -> Path:
    """Find the repository root by looking for .git directory."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists():
            return parent
    return cwd


def extract_links(content: str) -> list[tuple[str, str]]:
    """Extract all markdown links from content."""
    return LINK_PATTERN.findall(content)


def resolve_link(link: str, file_path: Path, repo_root: Path) -> Path | None:
    """Resolve a link to an absolute path."""
    # Skip URLs and anchors
    if any(link.startswith(proto) for proto in SKIP_PROTOCOLS):
        return None

    # Remove anchor from link
    link_path = link.split("#")[0]
    if not link_path:
        return None

    # Remove query strings
    link_path = link_path.split("?")[0]

    # Resolve relative to file's directory
    file_dir = file_path.parent

    if link_path.startswith("/"):
        # Root-relative path
        resolved = repo_root / link_path[1:]
    else:
        # Directory-relative path
        resolved = file_dir / link_path

    return resolved.resolve()


def check_file(file_path: Path, repo_root: Path) -> list[str]:
    """Check a single markdown file for broken links."""
    errors = []

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        errors.append(f"{file_path}: Could not read file: {e}")
        return errors

    links = extract_links(content)

    for _text, link in links:
        resolved = resolve_link(link, file_path, repo_root)
        if resolved is None:
            continue

        # Check if the resolved path exists
        if not resolved.exists():
            # Try following symlinks
            try:
                if resolved.is_symlink():
                    continue  # Skip broken symlinks silently for now
            except OSError:
                pass

            rel_path = file_path.relative_to(repo_root)
            errors.append(f"{rel_path}: Broken link '{link}' -> {resolved}")

    return errors


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: check_markdown_links.py <files...>", file=sys.stderr)
        return 1

    repo_root = get_repo_root()
    all_errors: list[str] = []

    for file_arg in sys.argv[1:]:
        file_path = Path(file_arg).resolve()
        if not file_path.exists():
            continue
        if file_path.suffix.lower() != ".md":
            continue
        # Skip files in excluded directories
        if any(skip_dir in file_path.parts for skip_dir in SKIP_DIRS):
            continue

        errors = check_file(file_path, repo_root)
        all_errors.extend(errors)

    if all_errors:
        print("Broken markdown links found:", file=sys.stderr)
        for error in all_errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
