#!/usr/bin/env python3
"""
Import lessons from agent network interchange format.

Part of Phase 4.3 Phase 1: Local Infrastructure
Design: knowledge/technical-designs/agent-network-protocol.md

Usage:
    ./scripts/lessons/import.py --source network-lessons/ --review
    ./scripts/lessons/import.py --lesson network-lessons/workflow/autonomous-run.md --adopt
"""

import sys
from pathlib import Path

import click
import yaml

from gptme_lessons_extras.network_schema import validate_network_metadata


def parse_network_lesson(content: str) -> tuple[dict | None, str]:
    """Parse network lesson into frontmatter and content.

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


def validate_network_lesson(
    frontmatter: dict,
    lesson_path: str,
) -> tuple[bool, list[str]]:
    """Validate a network lesson for import.

    Args:
        frontmatter: Lesson frontmatter dictionary
        lesson_path: Path to lesson file (for error messages)

    Returns:
        Tuple of (is_valid, error_messages)
    """
    errors = []

    # Check for network metadata
    if "network" not in frontmatter:
        errors.append("Missing network metadata section")
        return (False, errors)

    # Validate network metadata
    is_valid, validation_errors = validate_network_metadata(frontmatter["network"])
    if not is_valid:
        errors.extend(validation_errors)

    # Check for required lesson sections
    if "match" not in frontmatter:
        errors.append("Missing match section (keywords)")

    return (len(errors) == 0, errors)


def check_compatibility(
    network_metadata: dict,
    local_lessons_dir: Path,
) -> tuple[bool, str]:
    """Check if network lesson is compatible with local system.

    Args:
        network_metadata: Network metadata dictionary
        local_lessons_dir: Local lessons directory

    Returns:
        Tuple of (is_compatible, reason)
    """
    lesson_id = network_metadata.get("lesson_id", "unknown")

    # Check schema version compatibility
    schema_version = network_metadata.get("schema_version", "1.0")
    if schema_version != "1.0":
        return (False, f"Incompatible schema version: {schema_version}")

    # Check if lesson already exists locally
    # Extract category and slug from lesson_id: "{agent}-{category}-{slug}"
    parts = lesson_id.split("-", 2)
    if len(parts) >= 3:
        category = parts[1]
        slug = parts[2]

        # Check if lesson file exists
        potential_path = local_lessons_dir / category / f"{slug}.md"
        if potential_path.exists():
            return (False, f"Lesson already exists locally: {potential_path}")

    return (True, "Compatible")


def review_network_lesson(
    lesson_path: Path,
    local_lessons_dir: Path,
) -> dict:
    """Review a network lesson for adoption decision.

    Args:
        lesson_path: Path to network lesson file
        local_lessons_dir: Local lessons directory

    Returns:
        Dictionary with review information
    """
    # Read lesson
    content = lesson_path.read_text()

    # Parse
    frontmatter, markdown = parse_network_lesson(content)

    if frontmatter is None:
        return {
            "valid": False,
            "errors": ["No frontmatter found"],
            "compatible": False,
            "metadata": None,
        }

    # Validate
    is_valid, errors = validate_network_lesson(frontmatter, str(lesson_path))

    # Check compatibility
    is_compatible = False
    compat_reason = "Not checked"

    if is_valid:
        network_metadata = frontmatter.get("network", {})
        is_compatible, compat_reason = check_compatibility(
            network_metadata,
            local_lessons_dir,
        )

    return {
        "valid": is_valid,
        "errors": errors,
        "compatible": is_compatible,
        "compatibility_reason": compat_reason,
        "metadata": frontmatter.get("network") if frontmatter else None,
        "content_preview": markdown[:200] if markdown else None,
    }


def adopt_network_lesson(
    lesson_path: Path,
    local_lessons_dir: Path,
    force: bool = False,
) -> bool:
    """Adopt a network lesson into local system.

    Args:
        lesson_path: Path to network lesson file
        local_lessons_dir: Local lessons directory
        force: Force adoption even if exists locally

    Returns:
        True if successful, False otherwise
    """
    try:
        # Read lesson
        content = lesson_path.read_text()

        # Parse
        frontmatter, markdown = parse_network_lesson(content)

        if frontmatter is None:
            print(f"Error: No frontmatter in {lesson_path}")
            return False

        # Validate
        is_valid, errors = validate_network_lesson(frontmatter, str(lesson_path))
        if not is_valid:
            print(f"Error: Invalid lesson: {errors}")
            return False

        # Check compatibility
        network_metadata = frontmatter["network"]
        is_compatible, reason = check_compatibility(network_metadata, local_lessons_dir)

        if not is_compatible and not force:
            print(f"Error: {reason}")
            return False

        # Determine local path
        lesson_id = network_metadata["lesson_id"]
        parts = lesson_id.split("-", 2)
        if len(parts) < 3:
            print(f"Error: Invalid lesson_id format: {lesson_id}")
            return False

        category = parts[1]
        slug = parts[2]

        local_path = local_lessons_dir / category / f"{slug}.md"

        # Create category directory if needed
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Write lesson
        reconstructed = f"""---
{yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)}---

{markdown}
"""

        local_path.write_text(reconstructed)

        # Update adoption count in network metadata
        network_metadata["adoption_count"] = (
            network_metadata.get("adoption_count", 0) + 1
        )

        print(f"✓ Adopted: {lesson_id} → {local_path}")

        return True

    except Exception as e:
        print(f"Error adopting {lesson_path}: {e}")
        return False


@click.command()
@click.option(
    "--source",
    "-s",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Source directory with network lessons",
)
@click.option(
    "--lesson",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Import single lesson file",
)
@click.option(
    "--lessons-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default="lessons",
    help="Local lessons directory (default: lessons/)",
)
@click.option(
    "--review",
    is_flag=True,
    help="Review lessons without adopting",
)
@click.option(
    "--adopt",
    is_flag=True,
    help="Adopt reviewed lessons",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Force adoption even if exists locally",
)
def main(
    source: Path | None,
    lesson: Path | None,
    lessons_dir: Path,
    review: bool,
    adopt: bool,
    force: bool,
):
    """Import lessons from agent network interchange format."""
    print("Agent Network Lesson Import")
    print(f"Local lessons: {lessons_dir}")
    print()

    if lesson:
        # Import single lesson
        if review:
            # Review only
            review_result = review_network_lesson(lesson, lessons_dir)

            print(f"Review: {lesson.name}")
            print(f"  Valid: {review_result['valid']}")
            if review_result["errors"]:
                print(f"  Errors: {', '.join(review_result['errors'])}")
            print(f"  Compatible: {review_result['compatible']}")
            if not review_result["compatible"]:
                print(f"  Reason: {review_result['compatibility_reason']}")

            if review_result["metadata"]:
                print("\n  Metadata:")
                print(f"    ID: {review_result['metadata'].get('lesson_id')}")
                print(f"    Origin: {review_result['metadata'].get('agent_origin')}")
                print(f"    Confidence: {review_result['metadata'].get('confidence')}")
                print(
                    f"    Adoption count: {review_result['metadata'].get('adoption_count', 0)}"
                )

            if review_result["content_preview"]:
                print(f"\n  Preview: {review_result['content_preview']}...")

            sys.exit(0 if review_result["valid"] and review_result["compatible"] else 1)

        elif adopt:
            # Adopt lesson
            success = adopt_network_lesson(
                lesson,
                lessons_dir,
                force=force,
            )
            sys.exit(0 if success else 1)

        else:
            print("Error: Specify --review or --adopt")
            sys.exit(1)

    elif source:
        # Import from directory
        # Find all lesson files
        lesson_files = list(source.rglob("*.md"))
        lesson_files = [f for f in lesson_files if f.name != "README.md"]

        print(f"Found {len(lesson_files)} lessons in source")
        print()

        if review:
            # Review all
            for lesson_file in lesson_files:
                review_result = review_network_lesson(lesson_file, lessons_dir)

                status = (
                    "✓"
                    if review_result["valid"] and review_result["compatible"]
                    else "✗"
                )
                print(f"{status} {lesson_file.name}")

                if review_result["metadata"]:
                    print(f"   ID: {review_result['metadata'].get('lesson_id')}")
                    print(f"   Origin: {review_result['metadata'].get('agent_origin')}")

                if not review_result["valid"]:
                    print(f"   Errors: {', '.join(review_result['errors'])}")

                if not review_result["compatible"]:
                    print(f"   {review_result['compatibility_reason']}")

                print()

        elif adopt:
            # Adopt all compatible
            adopted = 0
            failed = 0

            for lesson_file in lesson_files:
                # Review first
                review_result = review_network_lesson(lesson_file, lessons_dir)

                if review_result["valid"] and review_result["compatible"]:
                    success = adopt_network_lesson(
                        lesson_file,
                        lessons_dir,
                        force=force,
                    )

                    if success:
                        adopted += 1
                    else:
                        failed += 1
                else:
                    print(f"⏭  Skipping {lesson_file.name} (not compatible)")

            print()
            print("Adoption complete:")
            print(f"  ✓ Adopted: {adopted}")
            print(f"  ✗ Failed: {failed}")

            sys.exit(0 if failed == 0 else 1)

        else:
            print("Error: Specify --review or --adopt")
            sys.exit(1)

    else:
        print("Error: Specify --source or --lesson")
        sys.exit(1)


if __name__ == "__main__":
    main()
