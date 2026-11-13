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
"""Compatibility entry point for task/tweet frontmatter validation.

New task hooks should use ``gptodo lint`` with the staged filenames.
Schema definitions live in gptodo, not in this wrapper.
"""

import sys
from pathlib import Path
from typing import List

import click
from rich.console import Console

# This script also runs through uv's isolated PEP 723 environment. Import the
# sibling source package so it uses the schema from this checkout, not an older
# globally installed gptodo (and requires no workspace installation).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages/gptodo/src"))

from gptodo.frontmatter_compat import frontmatter  # noqa: E402
from gptodo.utils import validate_task_file  # noqa: E402
from gptodo.validate_frontmatter import (  # noqa: E402
    validate_optional_fields,
    validate_schema,
)

console = Console(stderr=True)


def validate_frontmatter(file: Path, type_name: str = "tasks") -> List[str]:
    """Delegate task schema checks; preserve the legacy tweet invocation."""
    if type_name == "tasks":
        return validate_schema(file)
    try:
        post = frontmatter.load(file)
    except Exception as exc:
        return [f"Failed to parse frontmatter: {exc}"]
    return validate_task_file(
        file, post, type_name=type_name
    ) + validate_optional_fields(post.metadata, type_name=type_name)


@click.command()
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),  # type: ignore[type-var]
)
@click.option(
    "--type",
    "type_name",
    type=click.Choice(["tasks", "tweets"]),
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

    # click discards callback return values; errors must set the process status.
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
