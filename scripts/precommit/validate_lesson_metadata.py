#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "pyyaml",
# ]
# ///
"""
Pre-commit hook to validate lesson file metadata.

Ensures all lessons have:
- Valid YAML frontmatter
- match.keywords field with at least one keyword
- Proper structure for lesson matching to work

Usage:
    python3 scripts/precommit/validate_lesson_metadata.py [files...]
    python3 scripts/precommit/validate_lesson_metadata.py --all
"""

import sys
from pathlib import Path

import yaml


def get_frontmatter(content: str) -> tuple[dict | None, str | None]:
    """Extract YAML frontmatter from markdown content.

    Returns:
        Tuple of (frontmatter_dict, error_message)
    """
    if not content.startswith("---"):
        return None, "Missing YAML frontmatter (file must start with '---')"

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, "Malformed frontmatter (missing closing '---')"

    try:
        frontmatter = yaml.safe_load(parts[1])
        if not isinstance(frontmatter, dict):
            return None, "Frontmatter must be a YAML dictionary"
        return frontmatter, None
    except yaml.YAMLError as e:
        return None, f"Invalid YAML syntax: {e}"


def validate_lesson(path: Path) -> list[str]:
    """Validate a lesson file's metadata.

    Returns:
        List of error messages (empty if valid)
    """
    errors: list[str] = []

    if not path.exists():
        return [f"File not found: {path}"]

    content = path.read_text()

    # Check frontmatter exists and is valid
    frontmatter, error = get_frontmatter(content)
    if error:
        errors.append(error)
        return errors

    # Type narrowing: frontmatter is guaranteed non-None here since error was None
    assert frontmatter is not None

    # Check match.keywords exists
    if "match" not in frontmatter:
        errors.append("Missing 'match' field in frontmatter")
    elif not isinstance(frontmatter["match"], dict):
        errors.append("'match' field must be a dictionary")
    elif "keywords" not in frontmatter["match"]:
        errors.append("Missing 'match.keywords' field")
    elif not isinstance(frontmatter["match"]["keywords"], list):
        errors.append("'match.keywords' must be a list")
    elif len(frontmatter["match"]["keywords"]) == 0:
        errors.append("'match.keywords' must contain at least one keyword")
    else:
        # Validate keywords are non-empty strings
        for i, kw in enumerate(frontmatter["match"]["keywords"]):
            if not isinstance(kw, str):
                errors.append(
                    f"Keyword at index {i} must be a string, got {type(kw).__name__}"
                )
            elif not kw or not kw.strip():
                errors.append(f"Keyword at index {i} is empty or whitespace-only")

    # Optional: Check for valid status if present
    if "status" in frontmatter:
        valid_statuses = {"active", "automated", "deprecated", "archived", "draft"}
        if frontmatter["status"] not in valid_statuses:
            errors.append(
                f"Invalid status '{frontmatter['status']}'. "
                f"Valid values: {', '.join(sorted(valid_statuses))}"
            )

    # Check for deprecated metadata fields that should be computed, not committed
    # These fields cause unnecessary churn and should come from git history/trajectories
    deprecated_fields = {
        "lesson_id": "computed from path",
        "version": "tracked via git history",
        "usage_count": "computed from trajectories",
        "helpful_count": "computed from trajectories",
        "harmful_count": "computed from trajectories",
        "created": "use git log --follow --format=%aI --reverse -- <file> | head -1",
        "updated": "use git log -1 --format=%aI -- <file>",
        "last_used": "computed from trajectories",
    }
    found_deprecated = [f for f in deprecated_fields if f in frontmatter]
    if found_deprecated:
        for field in found_deprecated:
            errors.append(
                f"Deprecated field '{field}' should not be committed ({deprecated_fields[field]})"
            )

    # Check that content has a title (# Heading)
    content_after_frontmatter = content.split("---", 2)[2].strip()
    if not content_after_frontmatter.startswith("#"):
        errors.append("Lesson content must start with a markdown heading (# Title)")

    return errors


def find_lesson_files(repo_root: Path) -> list[Path]:
    """Find all lesson files in the repository."""
    lessons_dir = repo_root / "lessons"
    if not lessons_dir.exists():
        return []

    return [f for f in lessons_dir.glob("**/*.md") if f.name != "README.md"]


def main() -> int:
    """Main entry point for the validation script."""
    args = sys.argv[1:]

    if not args:
        print("Usage: validate_lesson_metadata.py [--all | files...]", file=sys.stderr)
        return 1

    # Determine which files to validate
    if "--all" in args:
        # Find repo root by looking for .git
        cwd = Path.cwd()
        repo_root = cwd
        while not (repo_root / ".git").exists():
            if repo_root.parent == repo_root:
                print("Error: Not in a git repository", file=sys.stderr)
                return 1
            repo_root = repo_root.parent

        files = find_lesson_files(repo_root)
        if not files:
            print("No lesson files found in lessons/ directory")
            return 0
    else:
        # Filter to only lesson files (in lessons/ directory)
        # Use proper path comparison instead of string matching
        cwd = Path.cwd()
        lessons_dir = cwd / "lessons"
        files = []
        for f in args:
            path = Path(f)
            # Check if file is under lessons/ directory and is markdown
            if f.endswith(".md") and not f.endswith("README.md"):
                try:
                    # Handle both absolute and relative paths
                    if path.is_absolute():
                        if path.is_relative_to(lessons_dir):
                            files.append(path)
                    else:
                        abs_path = (cwd / path).resolve()
                        if abs_path.is_relative_to(lessons_dir.resolve()):
                            files.append(path)
                except ValueError:
                    # is_relative_to raises ValueError if not relative
                    pass
        if not files:
            # No lesson files in the commit, nothing to validate
            return 0

    # Validate each file
    all_valid = True
    for file_path in files:
        errors = validate_lesson(file_path)
        if errors:
            all_valid = False
            print(f"\n❌ {file_path}:")
            for error in errors:
                print(f"   • {error}")

    if all_valid:
        if files:
            print(f"✅ All {len(files)} lesson file(s) have valid metadata")
        return 0
    else:
        print("\n" + "=" * 60)
        print("Lesson metadata validation failed!")
        print()
        print("Each lesson file must have YAML frontmatter with:")
        print("  ---")
        print("  match:")
        print("    keywords:")
        print("      - keyword1")
        print("      - keyword2")
        print("  ---")
        print()
        print("See lessons/workflow/git-workflow.md for a complete example.")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
