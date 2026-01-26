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

import argparse
from pathlib import Path
from typing import Optional

import yaml


from gptme_lessons_extras.network_schema import validate_network_metadata


def parse_network_lesson(content: str) -> tuple[Optional[dict], str]:
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


def main():
    parser = argparse.ArgumentParser(
        description="Import lessons from agent network interchange format"
    )
    parser.add_argument(
        "--source",
        "-s",
        type=Path,
        help="Source directory with network lessons",
    )
    parser.add_argument(
        "--lesson",
        type=Path,
        help="Import single lesson file",
    )
    parser.add_argument(
        "--lessons-dir",
        type=Path,
        default=Path("lessons"),
        help="Local lessons directory (default: lessons/)",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Review lessons without adopting",
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help="Adopt reviewed lessons",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force adoption even if exists locally",
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.lessons_dir.exists():
        print(f"Error: Lessons directory not found: {args.lessons_dir}")
        sys.exit(1)

    print("Agent Network Lesson Import")
    print(f"Local lessons: {args.lessons_dir}")
    print()

    if args.lesson:
        # Import single lesson
        if not args.lesson.exists():
            print(f"Error: Lesson file not found: {args.lesson}")
            sys.exit(1)

        if args.review:
            # Review only
            review = review_network_lesson(args.lesson, args.lessons_dir)

            print(f"Review: {args.lesson.name}")
            print(f"  Valid: {review['valid']}")
            if review["errors"]:
                print(f"  Errors: {', '.join(review['errors'])}")
            print(f"  Compatible: {review['compatible']}")
            if not review["compatible"]:
                print(f"  Reason: {review['compatibility_reason']}")

            if review["metadata"]:
                print("\n  Metadata:")
                print(f"    ID: {review['metadata'].get('lesson_id')}")
                print(f"    Origin: {review['metadata'].get('agent_origin')}")
                print(f"    Confidence: {review['metadata'].get('confidence')}")
                print(
                    f"    Adoption count: {review['metadata'].get('adoption_count', 0)}"
                )

            if review["content_preview"]:
                print(f"\n  Preview: {review['content_preview']}...")

            sys.exit(0 if review["valid"] and review["compatible"] else 1)

        elif args.adopt:
            # Adopt lesson
            success = adopt_network_lesson(
                args.lesson,
                args.lessons_dir,
                force=args.force,
            )
            sys.exit(0 if success else 1)

        else:
            print("Error: Specify --review or --adopt")
            sys.exit(1)

    elif args.source:
        # Import from directory
        if not args.source.exists():
            print(f"Error: Source directory not found: {args.source}")
            sys.exit(1)

        # Find all lesson files
        lesson_files = list(args.source.rglob("*.md"))
        lesson_files = [f for f in lesson_files if f.name != "README.md"]

        print(f"Found {len(lesson_files)} lessons in source")
        print()

        if args.review:
            # Review all
            for lesson_file in lesson_files:
                review = review_network_lesson(lesson_file, args.lessons_dir)

                status = "✓" if review["valid"] and review["compatible"] else "✗"
                print(f"{status} {lesson_file.name}")

                if review["metadata"]:
                    print(f"   ID: {review['metadata'].get('lesson_id')}")
                    print(f"   Origin: {review['metadata'].get('agent_origin')}")

                if not review["valid"]:
                    print(f"   Errors: {', '.join(review['errors'])}")

                if not review["compatible"]:
                    print(f"   {review['compatibility_reason']}")

                print()

        elif args.adopt:
            # Adopt all compatible
            adopted = 0
            failed = 0

            for lesson_file in lesson_files:
                # Review first
                review = review_network_lesson(lesson_file, args.lessons_dir)

                if review["valid"] and review["compatible"]:
                    success = adopt_network_lesson(
                        lesson_file,
                        args.lessons_dir,
                        force=args.force,
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
