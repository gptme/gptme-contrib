#!/usr/bin/env python3
"""
Lesson Generation Workflow

This script provides workflows for generating lessons from various sources:
1. Manual creation from template
2. From autonomous run logs (failure patterns)
3. From conversation analysis (learnable moments)
4. From GEPA trajectories (planned)

Usage:
    ./workflow.py create-from-template <title>
    ./workflow.py create-from-failure <log-file>
    ./workflow.py create-from-conversation <log-dir>
    ./workflow.py validate <lesson-file>
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class LessonTemplate:
    """Template for creating a new lesson."""

    title: str
    category: str
    keywords: list[str]
    tools: list[str]
    rule: str
    context: str
    failure_signals: list[str]
    anti_pattern: str
    recommended_pattern: str
    fix_recipe: list[str]
    rationale: str
    verification: list[str]
    exceptions: list[str]
    automation: list[str]
    origin: str


def create_lesson_from_template(title: str, category: str = "workflow") -> str:
    """Create a new lesson from template with proper YAML frontmatter."""
    template = f"""---
match:
  keywords: []
  tools: []
---

# {title}

## Rule
[One-sentence imperative constraint]

## Context
[When this applies]

## Failure Signals
- [Signal 1 (log/error/smell)]
- [Signal 2 (structural/code smell)]
- [Signal 3 (workflow symptom)]

## Anti-pattern (concise)
Don't: [describe the smell]
```text
# smell snippet (2-5 lines max)
```

## Recommended Pattern
Do: [minimal example or patch]
```text
# minimal before/after or correct snippet
```

## Fix Recipe
1. [Step 1]
2. [Step 2]
3. [Step 3]

## Rationale
[Why this matters (brief)]

## Verification Checklist
- [ ] [No <smell> present]
- [ ] [<Positive condition> holds]
- [ ] Tool/command: [quick check]

## Exceptions
- [Rare case 1 (why)]

## Automation Hooks
- Pre-commit: [script/check]
- Pattern: `[regex]` (to detect smell)

## Origin
[Where/when we learned this; link to PR/journal if relevant]

## Related
- [Link 1]
- [Link 2]
"""
    return template


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


def create_from_failure_log(log_file: Path) -> str | None:
    """
    Analyze a failure log and suggest a lesson structure.

    This is a placeholder for future AI-powered analysis.
    For now, it creates a template with hints from the log.
    """
    if not log_file.exists():
        print(f"Error: Log file not found: {log_file}")
        return None

    # Read last 100 lines of log
    with open(log_file) as f:
        lines = f.readlines()
        relevant_lines = lines[-100:]

    # Extract error patterns (basic heuristic)
    errors = [line for line in relevant_lines if "Error" in line or "Failed" in line]

    # Create lesson template with hints
    title = f"Handle {log_file.stem.replace('-', ' ').title()}"
    lesson = create_lesson_from_template(title, "workflow")

    # Add hints from log
    if errors:
        hints = "\n".join(f"- {error.strip()}" for error in errors[:5])
        lesson = lesson.replace(
            "## Failure Signals", f"## Failure Signals\n{hints}\n\n[Add more signals]"
        )

    return lesson


def main():
    import click

    @click.group()
    def cli():
        """Lesson generation workflow tools."""
        pass

    @cli.command("create-from-template")
    @click.argument("title")
    @click.option(
        "--category",
        default="workflow",
        help="Lesson category (default: workflow)",
    )
    @click.option(
        "--output",
        type=click.Path(),
        default=None,
        help="Output file path (default: stdout)",
    )
    def create_from_template(title, category, output):
        """Create new lesson from template."""
        lesson = create_lesson_from_template(title, category)
        if output:
            Path(output).write_text(lesson)
            print(f"Created lesson template: {output}")
        else:
            print(lesson)

    @cli.command("create-from-failure")
    @click.argument("log_file", type=click.Path(exists=True))
    @click.option(
        "--output",
        type=click.Path(),
        default=None,
        help="Output file path (default: stdout)",
    )
    def create_from_failure(log_file, output):
        """Create lesson from failure log."""
        lesson = create_from_failure_log(Path(log_file))
        if lesson:
            if output:
                Path(output).write_text(lesson)
                print(f"Created lesson from failure log: {output}")
            else:
                print(lesson)

    @cli.command()
    @click.argument("lesson_file", type=click.Path(exists=True))
    def validate(lesson_file):
        """Validate lesson file."""
        valid, errors = validate_lesson(Path(lesson_file))
        if valid:
            print(f"✅ Lesson is valid: {lesson_file}")
        else:
            print(f"❌ Lesson validation failed: {lesson_file}")
            for error in errors:
                print(f"  - {error}")
            sys.exit(1)

    cli()


if __name__ == "__main__":
    main()
