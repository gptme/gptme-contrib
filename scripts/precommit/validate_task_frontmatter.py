#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "click>=8.0.0",
#     "rich>=13.0.0",
#     "python-frontmatter>=1.1.0",
# ]
# [tool.uv]
# exclude-newer = "2024-04-01T00:00:00Z"
# ///
"""Validate YAML frontmatter in task/tweet files.

Checks:
- Required fields are present
- Fields have valid values
- Timestamps are in correct format
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import List

import click
import frontmatter
from rich.console import Console

# Configure console
console = Console(stderr=True)

VALID_STATES = {
    "tasks": ["new", "active", "paused", "done", "cancelled", "someday"],
    "tweets": ["new", "queued", "approved", "posted"],
}

VALID_PRIORITIES = ["low", "medium", "high"]
VALID_TASK_TYPES = ["project", "action"]


def validate_timestamp(ts: str | datetime) -> str | None:
    """Validate an ISO 8601 timestamp or datetime object."""
    if isinstance(ts, datetime):
        return None
    elif isinstance(ts, str):
        try:
            datetime.fromisoformat(ts)
            return None
        except ValueError:
            return f"Invalid timestamp format: {ts}"
    else:
        return f"Invalid timestamp type: {type(ts)}"


def validate_frontmatter(file: Path, type_name: str = "tasks") -> List[str]:
    """Validate frontmatter in a file."""
    errors = []

    try:
        post = frontmatter.load(file)
    except Exception as e:
        errors.append(f"Failed to parse frontmatter: {e}")
        return errors

    metadata = post.metadata

    # Check required fields
    required_fields = ["state", "created"]  # modified is optional, can use git history
    for field in required_fields:
        if field not in metadata:
            errors.append(f"Missing required field: {field}")

    # Validate state
    if "state" in metadata:
        state = metadata["state"]
        if state not in VALID_STATES[type_name]:
            errors.append(
                f"Invalid state: {state}. Must be one of: {', '.join(VALID_STATES[type_name])}"
            )

    # Validate timestamps
    for field in ["created", "modified"]:
        if field in metadata:
            if error := validate_timestamp(metadata[field]):
                errors.append(f"Field '{field}': {error}")

    # Validate optional fields
    if "priority" in metadata:
        priority = metadata["priority"]
        if priority not in VALID_PRIORITIES:
            errors.append(
                f"Invalid priority: {priority}. Must be one of: {', '.join(VALID_PRIORITIES)}"
            )

    if "task_type" in metadata:
        task_type = metadata["task_type"]
        if task_type not in VALID_TASK_TYPES:
            errors.append(
                f"Invalid task_type: {task_type}. Must be one of: {', '.join(VALID_TASK_TYPES)}"
            )

    if "tags" in metadata:
        tags = metadata["tags"]
        if not isinstance(tags, list):
            errors.append("Tags must be a list")

    if "depends" in metadata:
        depends = metadata["depends"]
        if not isinstance(depends, list):
            errors.append("Depends must be a list")

    # Validate waiting_for field
    if "waiting_for" in metadata:
        waiting_for = metadata["waiting_for"]
        if not isinstance(waiting_for, str):
            errors.append("waiting_for must be a string")
        elif not waiting_for.strip():
            errors.append("waiting_for cannot be empty")

    # Validate waiting_since timestamp
    if "waiting_since" in metadata:
        if error := validate_timestamp(metadata["waiting_since"]):
            errors.append(f"Field 'waiting_since': {error}")

    # Check waiting_since requires waiting_for
    if "waiting_since" in metadata and "waiting_for" not in metadata:
        errors.append("waiting_since requires waiting_for field to be set")

    return errors


@click.command()
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),  # type: ignore
)
@click.option(
    "--type",
    "type_name",
    type=click.Choice(list(VALID_STATES.keys())),
    default="tasks",
    help="Type of files to validate",
)
def main(files: List[Path], type_name: str = "tasks") -> int:
    """Validate YAML frontmatter in task/tweet files."""
    exit_code = 0

    for file in files:
        if errors := validate_frontmatter(file, type_name):
            console.print(f"[red]Errors in {file}:")
            for error in errors:
                console.print(f"  - {error}")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
