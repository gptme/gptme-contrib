#!/usr/bin/env python3
"""
Create a pull request for a lesson file.

Usage:
    ./create-pr.py <lesson-file> [--scores <scores-file>] [--conversation <link>] [--dry-run]
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


def validate_lesson(lesson_path: Path) -> tuple[bool, list[str]]:
    """
    Validate a lesson file has required sections and proper format.

    Returns:
        (valid, errors) where valid is True if lesson is valid, errors is list of issues
    """
    errors = []

    # Check file exists
    if not lesson_path.exists():
        return False, [f"File not found: {lesson_path}"]

    content = lesson_path.read_text()

    # Check for YAML frontmatter
    if not content.startswith("---\n"):
        errors.append("Missing YAML frontmatter (should start with '---')")

    # Parse frontmatter
    try:
        parts = content.split("---\n", 2)
        if len(parts) < 3:
            errors.append("Invalid frontmatter structure")
        else:
            frontmatter = yaml.safe_load(parts[1])
            if not isinstance(frontmatter, dict):
                errors.append("Frontmatter is not a dictionary")
            else:
                # Check for match section
                if "match" not in frontmatter:
                    errors.append("Missing 'match' section in frontmatter")
                else:
                    match = frontmatter["match"]
                    if not isinstance(match, dict):
                        errors.append("'match' section must be a dictionary")
                    else:
                        # Check for keywords or tools
                        has_keywords = bool(match.get("keywords"))
                        has_tools = bool(match.get("tools"))
                        if not (has_keywords or has_tools):
                            errors.append(
                                "Lesson must have at least one keyword or tool in match section"
                            )
    except yaml.YAMLError as e:
        errors.append(f"YAML parsing error: {e}")

    # Check for required sections
    required_sections = [
        "# ",  # Title
        "## Rule",
        "## Context",
        "## Failure Signals",
        "## Anti-pattern",
        "## Recommended Pattern",
        "## Fix Recipe",
        "## Rationale",
        "## Verification Checklist",
        "## Origin",
    ]

    for section in required_sections:
        if section not in content:
            errors.append(f"Missing required section: {section}")

    return len(errors) == 0, errors


def create_lesson_pr(
    lesson_file: Path,
    judge_scores: dict | None = None,
    conversation_link: str | None = None,
    dry_run: bool = False,
) -> bool:
    """
    Create a PR for a lesson file.

    Steps:
    1. Validate the lesson
    2. Create a git branch
    3. Commit the lesson
    4. Push the branch
    5. Create a PR with context

    Returns True if successful, False otherwise.
    """
    # Get repo root from git
    try:
        repo_root = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], text=True
            ).strip()
        )
    except subprocess.CalledProcessError:
        print("❌ Not in a git repository")
        return False

    # Validate lesson first
    valid, errors = validate_lesson(lesson_file)
    if not valid:
        print("❌ Lesson validation failed:")
        for error in errors:
            print(f"  - {error}")
        return False

    # Parse lesson to extract title and category
    content = lesson_file.read_text()
    title_match = re.search(r"^# (.+)$", content, re.MULTILINE)
    if not title_match:
        print("❌ Could not extract lesson title")
        return False

    title = title_match.group(1)

    # Determine category from file path
    # Assume path is like lessons/workflow/my-lesson.md or lessons/tools/my-tool.md
    try:
        relative_path = lesson_file.relative_to(repo_root)
        parts = relative_path.parts
        if len(parts) >= 2 and parts[0] == "lessons":
            category = parts[1]
        else:
            category = "workflow"  # default
    except ValueError:
        category = "workflow"

    # Create slug from filename
    slug = lesson_file.stem

    # Create branch name
    branch_name = f"lesson/{category}-{slug}"

    print(f"📝 Creating PR for lesson: {title}")
    print(f"   Category: {category}")
    print(f"   Branch: {branch_name}")
    print(f"   File: {relative_path}")

    if dry_run:
        print("🔍 Dry run - would create PR but not actually doing it")
        return True

    try:
        # Store current branch
        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root,
        ).stdout.strip()

        # Create and checkout branch
        print(f"📌 Creating branch: {branch_name}")
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            check=True,
            cwd=repo_root,
        )

        # Add the lesson file
        print(f"➕ Adding file: {relative_path}")
        subprocess.run(
            ["git", "add", str(relative_path)],
            check=True,
            cwd=repo_root,
        )

        # Commit
        commit_msg = f"feat(lessons): add {title}"
        print(f"💾 Committing: {commit_msg}")
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            check=True,
            cwd=repo_root,
        )

        # Push branch
        print(f"🚀 Pushing branch: {branch_name}")
        subprocess.run(
            ["git", "push", "origin", branch_name],
            check=True,
            cwd=repo_root,
        )

        # Create PR body
        pr_body = f"""## New Lesson: {title}

**Category**: {category}
**File**: `{relative_path}`

### Validation
✅ Lesson validated successfully

"""

        if judge_scores:
            pr_body += "### Judge Scores\n"
            pr_body += "```yaml\n"
            pr_body += yaml.dump(judge_scores, default_flow_style=False)
            pr_body += "```\n\n"

        if conversation_link:
            pr_body += f"""### Context
**Conversation**: {conversation_link}

"""

        pr_body += """### Review Checklist
- [ ] Lesson follows template format
- [ ] Rule is clear and actionable
- [ ] Failure signals are specific and detectable
- [ ] Fix recipe is step-by-step
- [ ] Verification checklist is testable
- [ ] Automation hooks are implementable

"""

        # Create PR
        print("📄 Creating pull request...")
        pr_result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                commit_msg,
                "--body",
                pr_body,
                "--base",
                "master",
                "--head",
                branch_name,
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )

        pr_url = pr_result.stdout.strip()
        print(f"✅ PR created: {pr_url}")

        # Return to previous branch
        print(f"🔙 Returning to branch: {current_branch}")
        subprocess.run(
            ["git", "checkout", current_branch],
            check=True,
            cwd=repo_root,
        )

        return True

    except subprocess.CalledProcessError as e:
        print(f"❌ Error creating PR: {e}")
        if hasattr(e, "stderr") and e.stderr:
            print(f"   Error output: {e.stderr}")

        # Try to clean up - return to previous branch
        try:
            subprocess.run(
                ["git", "checkout", current_branch],
                check=False,
                cwd=repo_root,
            )
        except Exception:
            pass

        return False


def main():
    parser = argparse.ArgumentParser(
        description="Create a pull request for a lesson file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "lesson_file",
        type=Path,
        help="Path to lesson file",
    )
    parser.add_argument(
        "--scores",
        type=Path,
        help="Path to judge scores JSON file",
    )
    parser.add_argument(
        "--conversation",
        type=str,
        help="Link to conversation that generated the lesson",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show what would be done, but don't create PR",
    )

    args = parser.parse_args()

    # Load judge scores if provided
    judge_scores = None
    if args.scores:
        if not args.scores.exists():
            print(f"❌ Scores file not found: {args.scores}")
            return 1
        try:
            with open(args.scores) as f:
                judge_scores = json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ Error parsing scores file: {e}")
            return 1

    # Create PR
    success = create_lesson_pr(
        args.lesson_file,
        judge_scores=judge_scores,
        conversation_link=args.conversation,
        dry_run=args.dry_run,
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
