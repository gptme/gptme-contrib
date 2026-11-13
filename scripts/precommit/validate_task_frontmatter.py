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
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import List, NamedTuple, cast

import click
import frontmatter
from rich.console import Console

# Configure console
console = Console(stderr=True)

# Task states and priorities are NOT defined here: gptodo owns the task schema
# (see gptodo.utils.get_canonical_states / DEPRECATED_STATE_ALIASES /
# VALID_PRIORITIES) and they are loaded from it at runtime by
# _load_task_schema(). A second hard-coded copy drifted before (gptme-contrib#2161:
# `gptodo expire` writes `state: expired`, which this hook used to reject).
VALID_TWEET_STATES = ["new", "queued", "approved", "posted"]
FILE_TYPES = ["tasks", "tweets"]

# gptodo source tree in the same gptme-contrib checkout as this script
# (scripts/precommit/ -> packages/gptodo/src). Preferred over an installed gptodo
# so the hook always agrees with the gptodo version it ships with.
_GPTODO_SRC = Path(__file__).resolve().parents[2] / "packages" / "gptodo" / "src"


class TaskSchema(NamedTuple):
    states: list[str]  # every accepted value (canonical + deprecated aliases)
    canonical_states: list[str]  # what to suggest in error messages
    priorities: list[str]


@cache
def _load_task_schema() -> TaskSchema:
    """Load the task schema from gptodo, the single source of truth.

    Fails loudly (exit 2) instead of falling back to a local copy: a silent
    fallback is exactly how the two definitions drifted apart.
    """
    if _GPTODO_SRC.is_dir() and str(_GPTODO_SRC) not in sys.path:
        sys.path.insert(0, str(_GPTODO_SRC))
    try:
        from gptodo.utils import (  # type: ignore[import-not-found]  # noqa: PLC0415
            DEPRECATED_STATE_ALIASES,
            VALID_PRIORITIES,
            get_canonical_states,
        )
    except ImportError as e:
        console.print(
            "[red]Cannot load the task schema from gptodo "
            f"({e}).[/red]\n"
            f"Expected the gptodo source at {_GPTODO_SRC} (run this script from "
            "its gptme-contrib checkout) or an up-to-date installed `gptodo` package."
        )
        sys.exit(2)
    canonical = list(get_canonical_states())
    aliases = [s for s in DEPRECATED_STATE_ALIASES if s not in canonical]
    return TaskSchema(
        states=canonical + aliases,
        canonical_states=canonical,
        priorities=list(VALID_PRIORITIES),
    )


VALID_TASK_TYPES = ["project", "action"]

# Synonym map: agent-intuitive guesses → canonical GTD values
TASK_TYPE_SYNONYMS = {
    "research": "action",
    "investigation": "action",
    "chore": "action",
    "bug": "action",
    "decision": "action",
    "task": "action",
    "epic": "project",
}


def validate_timestamp(ts: str | datetime | date) -> str | None:
    """Validate an ISO 8601 timestamp or datetime/date object.

    YAML parses a bare ``2026-06-24`` value into a ``datetime.date`` (not a
    ``datetime`` or ``str``). A date is a valid ISO 8601 timestamp at date
    precision, so accept it rather than rejecting it as an invalid type.
    """
    # datetime is a subclass of date, so this covers both object forms.
    if isinstance(ts, date):
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

    # Enum schemas: tasks come from gptodo, tweets are local to this hook
    if type_name == "tasks":
        schema = _load_task_schema()
        valid_states, suggested_states = schema.states, schema.canonical_states
        valid_priorities = schema.priorities
    else:
        valid_states = suggested_states = VALID_TWEET_STATES
        # Tweets have no priority schema of their own; reuse gptodo's only if
        # a tweet actually sets one, so tweets without one never need gptodo.
        valid_priorities = (
            _load_task_schema().priorities if "priority" in metadata else []
        )

    # Validate state
    if "state" in metadata:
        state = metadata["state"]
        if state not in valid_states:
            errors.append(
                f"Invalid state: {state}. Must be one of: {', '.join(suggested_states)}"
            )

    # Validate timestamps
    for field in ["created", "modified"]:
        if field in metadata:
            if error := validate_timestamp(
                cast("str | datetime | date", metadata[field])
            ):
                errors.append(f"Field '{field}': {error}")

    # Validate optional fields
    if "priority" in metadata:
        priority = metadata["priority"]
        if priority is not None and priority not in valid_priorities:
            errors.append(
                f"Invalid priority: {priority}. Must be one of: {', '.join(valid_priorities)}"
            )

    if "task_type" in metadata:
        task_type = metadata["task_type"]
        if task_type not in VALID_TASK_TYPES:
            suggestion = ""
            task_type_lower = str(task_type).lower()
            if task_type_lower in TASK_TYPE_SYNONYMS:
                canonical = TASK_TYPE_SYNONYMS[task_type_lower]
                suggestion = f" — did you mean `task_type: {canonical}`?"
            elif task_type_lower in VALID_TASK_TYPES:
                suggestion = f" — did you mean `task_type: {task_type_lower}`?"
            template_hint = (
                " See tasks/templates/default.md for examples."
                if type_name == "tasks"
                else ""
            )
            errors.append(
                f"Invalid task_type: {task_type}. Must be one of: {', '.join(VALID_TASK_TYPES)}{suggestion}.{template_hint}"
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
        if error := validate_timestamp(
            cast("str | datetime | date", metadata["waiting_since"])
        ):
            errors.append(f"Field 'waiting_since': {error}")

    # Check waiting_since requires waiting_for
    if "waiting_since" in metadata and "waiting_for" not in metadata:
        errors.append("waiting_since requires waiting_for field to be set")

    return errors


@click.command()
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),  # type: ignore[type-var]
)
@click.option(
    "--type",
    "type_name",
    type=click.Choice(FILE_TYPES),
    default="tasks",
    help="Type of files to validate",
)
def main(files: List[Path], type_name: str = "tasks") -> None:
    """Validate YAML frontmatter in task/tweet files."""
    exit_code = 0

    for file in files:
        if errors := validate_frontmatter(file, type_name):
            console.print(f"[red]Errors in {file}:")
            for error in errors:
                console.print(f"  - {error}")
            exit_code = 1

    # click discards return values from command callbacks, so we must call
    # sys.exit() directly. Without this, the precommit hook silently passes
    # even when validate_frontmatter() reports errors.
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
