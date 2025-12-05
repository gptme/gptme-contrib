#!/usr/bin/env python3
"""
Export lessons to agent network interchange format.

Part of Phase 4.3 Phase 1: Local Infrastructure
Design: knowledge/technical-designs/agent-network-protocol.md

Usage:
    ./scripts/lessons/export.py --output network-lessons/
    ./scripts/lessons/export.py --lesson lessons/workflow/autonomous-run.md
"""

import sys

import argparse
import os
from pathlib import Path
from typing import Optional

import yaml


from lessons.network_schema import NetworkMetadata, validate_network_metadata


def parse_lesson_frontmatter(content: str) -> tuple[Optional[dict], str]:
    """Parse lesson frontmatter and content.

    Args:
        content: Full lesson file content

    Returns:
        Tuple of (frontmatter_dict, markdown_content)
    """
    if not content.startswith("---"):
        return None, content

    # Find end of frontmatter
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content

    try:
        frontmatter = yaml.safe_load(parts[1])
        markdown = parts[2].strip()
        return frontmatter, markdown
    except yaml.YAMLError:
        return None, content


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
) -> bool:
    """Export a single lesson to interchange format.

    Args:
        lesson_path: Path to lesson file
        output_dir: Directory to write exported lesson
        agent_origin: Which agent created this
        force: Overwrite existing exports

    Returns:
        True if successful, False otherwise
    """
    try:
        # Read lesson
        content = lesson_path.read_text()

        # Parse frontmatter
        frontmatter, markdown = parse_lesson_frontmatter(content)

        if frontmatter is None:
            print(f"Warning: No frontmatter in {lesson_path}, skipping")
            return False

        # Add network metadata
        frontmatter = add_network_metadata(
            frontmatter=frontmatter,
            lesson_path=str(lesson_path),
            agent_origin=agent_origin,
        )

        # Construct export format
        export_content = f"""---
{yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)}---

{markdown}
"""

        # Write to output directory
        # Preserve category structure: workflow/autonomous-run.md
        relative_path = lesson_path.relative_to(lesson_path.parent.parent)
        output_path = output_dir / relative_path

        # Create parent directories
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if exists
        if output_path.exists() and not force:
            print(
                f"Skipping {lesson_path.name} (already exported, use --force to overwrite)"
            )
            return True

        # Write
        output_path.write_text(export_content)

        lesson_id = frontmatter["network"]["lesson_id"]
        print(f"✓ Exported: {lesson_path.name} → {lesson_id}")

        return True

    except Exception as e:
        print(f"Error exporting {lesson_path}: {e}")
        return False


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
        success = export_lesson(
            lesson_path=lesson_path,
            output_dir=output_dir,
            agent_origin=agent_origin,
            force=force,
        )

        if success:
            counts["success"] += 1
        else:
            counts["failed"] += 1

    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Export lessons to agent network interchange format"
    )
    parser.add_argument(
        "--lessons-dir",
        type=Path,
        default=Path("lessons"),
        help="Lessons directory to export from (default: lessons/)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("network-lessons"),
        help="Output directory for exported lessons (default: network-lessons/)",
    )
    parser.add_argument(
        "--lesson",
        type=Path,
        help="Export single lesson file instead of all",
    )
    parser.add_argument(
        "--agent",
        default="agent",
        help="Agent origin identifier (default: agent)",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing exports",
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.lessons_dir.exists():
        print(f"Error: Lessons directory not found: {args.lessons_dir}")
        sys.exit(1)

    # Create output directory
    args.output.mkdir(parents=True, exist_ok=True)

    print("Agent Network Lesson Export")
    print(f"Agent: {args.agent}")
    print(f"Output: {args.output}")
    print()

    if args.lesson:
        # Export single lesson
        if not args.lesson.exists():
            print(f"Error: Lesson file not found: {args.lesson}")
            sys.exit(1)

        success = export_lesson(
            lesson_path=args.lesson,
            output_dir=args.output,
            agent_origin=args.agent,
            force=args.force,
        )

        sys.exit(0 if success else 1)

    else:
        # Export all lessons
        counts = export_all_lessons(
            lessons_dir=args.lessons_dir,
            output_dir=args.output,
            agent_origin=args.agent,
            force=args.force,
        )

        print()
        print("Export complete:")
        print(f"  ✓ Success: {counts['success']}")
        print(f"  ⏭  Skipped: {counts['skipped']}")
        print(f"  ✗ Failed: {counts['failed']}")

        sys.exit(0 if counts["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
