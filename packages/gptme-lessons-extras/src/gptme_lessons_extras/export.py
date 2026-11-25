#!/usr/bin/env python3
"""
Export lessons to agent network interchange format.

Part of Phase 4.3 Phase 1: Local Infrastructure
Design: knowledge/technical-designs/agent-network-protocol.md

Usage:
    ./scripts/lessons/export.py --output network-lessons/
    ./scripts/lessons/export.py --lesson lessons/workflow/autonomous-run.md
"""

import os
import sys
from pathlib import Path

import click
import yaml
from bobutils.frontmatter import split_frontmatter

from gptme_lessons_extras.network_schema import (
    NetworkMetadata,
    validate_network_metadata,
)


def parse_lesson_frontmatter(content: str) -> tuple[dict | None, str]:
    """Parse lesson frontmatter and content.

    Args:
        content: Full lesson file content

    Returns:
        Tuple of (frontmatter_dict, markdown_content)
    """
    try:
        raw_yaml, markdown = split_frontmatter(content)
        frontmatter = yaml.safe_load(raw_yaml)
    except (ValueError, yaml.YAMLError):
        return None, content
    if not isinstance(frontmatter, dict):
        return None, content
    return frontmatter, markdown.strip()


def add_network_metadata(
    frontmatter: dict,
    lesson_path: str,
    agent_origin: str = "agent",
) -> dict:
    """Add network metadata to lesson frontmatter.

    Args:
        frontmatter: Existing frontmatter dictionary
        lesson_path: Path to lesson file (for ID generation)
        agent_origin: Which agent created this

    Returns:
        Updated frontmatter with network section
    """
    # If network metadata already exists, validate and return
    if "network" in frontmatter:
        is_valid, errors = validate_network_metadata(frontmatter["network"])
        if is_valid:
            return frontmatter
        else:
            print(f"Warning: Invalid network metadata in {lesson_path}: {errors}")
            print("Regenerating network metadata...")

    # Generate default network metadata
    metadata = NetworkMetadata.generate_default(
        lesson_path=lesson_path,
        agent_origin=agent_origin,
    )

    # Add to frontmatter
    frontmatter["network"] = metadata.to_dict()

    return frontmatter


def export_lesson(
    lesson_path: Path,
    output_dir: Path,
    agent_origin: str = "agent",
    force: bool = False,
    lessons_root: Path | None = None,
) -> bool:
    """Export a single lesson to interchange format.

    Args:
        lesson_path: Path to lesson file
        output_dir: Directory to write exported lesson
        agent_origin: Which agent created this
        force: Overwrite existing exports
        lessons_root: Lessons directory the category path is taken relative
            to. Defaults to the lesson's grandparent (one category level).

    Returns:
        True if exported or already exported, False on failure
    """
    status = _export_lesson(lesson_path, output_dir, agent_origin, force, lessons_root)
    return status != "failed"


def _export_lesson(
    lesson_path: Path,
    output_dir: Path,
    agent_origin: str,
    force: bool,
    lessons_root: Path | None,
) -> str:
    """Export one lesson; return "success", "skipped" or "failed"."""
    try:
        # Read lesson
        content = lesson_path.read_text()

        # Parse frontmatter
        frontmatter, markdown = parse_lesson_frontmatter(content)

        if frontmatter is None:
            print(f"Warning: No frontmatter in {lesson_path}, skipping")
            return "failed"

        # Preserve the full category structure: tools/shell/quoting.md
        root = lessons_root if lessons_root is not None else lesson_path.parent.parent
        relative_path = lesson_path.relative_to(root)
        output_path = output_dir / relative_path

        # Check if exists
        if output_path.exists() and not force:
            print(
                f"Skipping {lesson_path.name} (already exported, use --force to overwrite)"
            )
            return "skipped"

        # Escape category separators (and literal percent signs), so tools/shell
        # cannot collide with tools-shell or tools%2Fshell. Nothing else is
        # encoded, so other single-level category ids stay unchanged.
        id_path = lesson_path
        if relative_path.parent != Path("."):
            category = relative_path.parent.as_posix()
            category = category.replace("%", "%25").replace("/", "%2F")
            id_path = Path(category) / relative_path.name

        # Add network metadata
        frontmatter = add_network_metadata(
            frontmatter=frontmatter,
            lesson_path=str(id_path),
            agent_origin=agent_origin,
        )

        # Construct export format
        export_content = f"""---
{yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)}---

{markdown}
"""

        # Write
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(export_content)

        lesson_id = frontmatter["network"]["lesson_id"]
        print(f"✓ Exported: {lesson_path.name} → {lesson_id}")

        return "success"

    except Exception as e:
        print(f"Error exporting {lesson_path}: {e}")
        return "failed"


def _locate_single_lesson(lesson: Path, lessons_dir: Path) -> tuple[Path, Path | None]:
    """Pick the path and lessons root for a single-file export.

    Returns a root of None for a lesson outside ``lessons_dir``, which then
    exports relative to its grandparent.
    """
    # abspath keeps symlinks, so a linked lesson stays in the category it is
    # filed under. It also collapses ".." textually, which names another file
    # if the ".." follows a symlink.
    as_written = Path(os.path.abspath(lesson))
    if not (as_written.exists() and as_written.samefile(lesson)):
        as_written = lesson.resolve()

    real_root = lessons_dir.resolve()
    for path, root in (
        (as_written, Path(os.path.abspath(lessons_dir))),
        # Same directory spelled through a different symlink
        (lesson.parent.resolve() / lesson.name, real_root),
        # A link from elsewhere to a lesson inside the root
        (lesson.resolve(), real_root),
    ):
        if path.is_relative_to(root):
            return path, root
    return as_written, None


def export_all_lessons(
    lessons_dir: Path,
    output_dir: Path,
    agent_origin: str = "agent",
    force: bool = False,
) -> dict[str, int]:
    """Export all lessons from lessons directory.

    Args:
        lessons_dir: Root lessons directory
        output_dir: Directory to write exports
        agent_origin: Which agent created these
        force: Overwrite existing exports

    Returns:
        Dictionary with counts: {success, skipped, failed}
    """
    counts = {"success": 0, "skipped": 0, "failed": 0}

    # Find all lesson files
    lesson_files = []
    for root, dirs, files in os.walk(lessons_dir):
        # Skip templates and README
        dirs[:] = [d for d in dirs if d != "templates"]

        for file in files:
            if file.endswith(".md") and file != "README.md":
                lesson_files.append(Path(root) / file)

    print(f"Found {len(lesson_files)} lessons to export")
    print()

    # Export each lesson
    for lesson_path in sorted(lesson_files):
        status = _export_lesson(
            lesson_path, output_dir, agent_origin, force, lessons_root=lessons_dir
        )
        counts[status] += 1

    return counts


@click.command()
@click.option(
    "--lessons-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default="lessons",
    help="Lessons directory to export from (default: lessons/)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default="network-lessons",
    help="Output directory for exported lessons (default: network-lessons/)",
)
@click.option(
    "--lesson",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Export single lesson file instead of all",
)
@click.option(
    "--agent",
    default="agent",
    help="Agent origin identifier (default: agent)",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing exports",
)
def main(lessons_dir: Path, output: Path, lesson: Path | None, agent: str, force: bool):
    """Export lessons to agent network interchange format."""
    # Create output directory
    output.mkdir(parents=True, exist_ok=True)

    print("Agent Network Lesson Export")
    print(f"Agent: {agent}")
    print(f"Output: {output}")
    print()

    if lesson:
        # Export single lesson
        lesson_path, lessons_root = _locate_single_lesson(lesson, lessons_dir)
        success = export_lesson(
            lesson_path=lesson_path,
            output_dir=output,
            agent_origin=agent,
            force=force,
            lessons_root=lessons_root,
        )

        sys.exit(0 if success else 1)

    else:
        # Export all lessons
        counts = export_all_lessons(
            lessons_dir=lessons_dir,
            output_dir=output,
            agent_origin=agent,
            force=force,
        )

        print()
        print("Export complete:")
        print(f"  ✓ Success: {counts['success']}")
        print(f"  ⏭  Skipped: {counts['skipped']}")
        print(f"  ✗ Failed: {counts['failed']}")

        sys.exit(0 if counts["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
