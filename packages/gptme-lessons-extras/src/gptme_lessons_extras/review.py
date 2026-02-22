#!/usr/bin/env python3
"""
Lesson review interface for agent network protocol.

Provides tools to:
- List available lessons from network
- Show lesson metadata and preview
- Compare with local lessons
- Recommend lessons for adoption
"""

import sys
from pathlib import Path
from typing import Any


def list_network_lessons(network_dir: Path, agent: str) -> list[dict[str, Any]]:
    """
    List all available lessons from other agents in the network.

    Args:
        network_dir: Path to network repository
        agent: Current agent name (to exclude own lessons)

    Returns:
        List of lesson metadata dicts
    """
    lessons = []

    # Scan all agent directories except current agent
    for agent_dir in network_dir.iterdir():
        if (
            not agent_dir.is_dir()
            or agent_dir.name == agent
            or agent_dir.name.startswith(".")
        ):
            continue

        # Scan all category directories
        for category_dir in agent_dir.iterdir():
            if not category_dir.is_dir():
                continue

            # Find all lesson files
            for lesson_file in category_dir.glob("*.md"):
                try:
                    # Parse lesson to extract metadata
                    with open(lesson_file) as f:
                        content = f.read()

                    # Simple frontmatter extraction
                    if content.startswith("---\n"):
                        end_idx = content.find("\n---\n", 4)
                        if end_idx != -1:
                            import yaml

                            frontmatter_str = content[4:end_idx]
                            metadata = yaml.safe_load(frontmatter_str)

                            # Add file info
                            metadata["_file"] = str(
                                lesson_file.relative_to(network_dir)
                            )
                            metadata["_agent"] = agent_dir.name
                            metadata["_category"] = category_dir.name

                            lessons.append(metadata)
                except Exception as e:
                    print(
                        f"Warning: Failed to parse {lesson_file}: {e}", file=sys.stderr
                    )
                    continue

    return lessons


def show_lesson_preview(lesson_file: Path, lines: int = 20) -> str:
    """
    Show preview of lesson content.

    Args:
        lesson_file: Path to lesson file
        lines: Number of lines to show

    Returns:
        Preview string
    """
    try:
        with open(lesson_file) as f:
            content_lines = f.readlines()

        # Skip frontmatter
        start = 0
        if content_lines[0].strip() == "---":
            for i, line in enumerate(content_lines[1:], 1):
                if line.strip() == "---":
                    start = i + 1
                    break

        preview_lines = content_lines[start : start + lines]
        preview = "".join(preview_lines)

        if len(content_lines) > start + lines:
            preview += f"\n... ({len(content_lines) - start - lines} more lines)"

        return preview
    except Exception as e:
        return f"Error reading file: {e}"


def compare_with_local(
    network_metadata: dict[str, Any], lessons_dir: Path
) -> dict[str, Any]:
    """
    Compare network lesson with local lessons.

    Args:
        network_metadata: Network lesson metadata
        lessons_dir: Local lessons directory

    Returns:
        Comparison result dict
    """
    result = {
        "exists_locally": False,
        "local_path": None,
        "conflict": False,
        "recommendation": "adopt",
    }

    # Extract lesson_id from network metadata
    network_id = network_metadata.get("network", {}).get("lesson_id")
    if not network_id:
        result["recommendation"] = "skip"
        result["reason"] = "Missing lesson_id"
        return result

    # Construct expected local path from lesson_id
    # Format: agent-category-slug
    parts = network_id.split("-", 2)
    if len(parts) < 3:
        result["recommendation"] = "skip"
        result["reason"] = "Invalid lesson_id format"
        return result

    _, category, slug = parts
    local_path = lessons_dir / category / f"{slug}.md"

    if local_path.exists():
        result["exists_locally"] = True
        result["local_path"] = str(local_path)

        # Check for conflicts (same slug, different content)
        # For now, mark as conflict if exists
        result["conflict"] = True
        result["recommendation"] = "review"
        result["reason"] = "Local lesson with same ID exists"

    return result


def recommend_lessons(
    lessons: list[dict[str, Any]],
    lessons_dir: Path,
    min_confidence: float = 0.7,
    min_adoption: int = 0,
) -> list[dict[str, Any]]:
    """
    Recommend lessons for adoption based on criteria.

    Args:
        lessons: List of network lesson metadata
        lessons_dir: Local lessons directory
        min_confidence: Minimum confidence threshold
        min_adoption: Minimum adoption count

    Returns:
        List of recommended lessons with rationale
    """
    recommendations = []

    for lesson in lessons:
        network_meta = lesson.get("network", {})
        confidence = network_meta.get("confidence", 0.0)
        adoption_count = network_meta.get("adoption_count", 0)

        # Check automatic adoption criteria
        if confidence < min_confidence:
            continue
        if adoption_count < min_adoption:
            continue

        # Compare with local lessons
        comparison = compare_with_local(lesson, lessons_dir)

        if comparison["recommendation"] == "adopt":
            recommendations.append(
                {
                    "lesson": lesson,
                    "comparison": comparison,
                    "rationale": f"High quality (conf={confidence:.2f}), no conflicts",
                }
            )
        elif comparison["recommendation"] == "review":
            recommendations.append(
                {
                    "lesson": lesson,
                    "comparison": comparison,
                    "rationale": comparison.get("reason", "Manual review needed"),
                }
            )

    return recommendations


def print_lesson_summary(lesson: dict[str, Any]):
    """Print formatted lesson summary."""
    network_meta = lesson.get("network", {})

    print(f"Lesson: {network_meta.get('lesson_id', 'unknown')}")
    print(f"  Agent: {lesson.get('_agent', 'unknown')}")
    print(f"  Category: {lesson.get('_category', 'unknown')}")
    print(f"  Confidence: {network_meta.get('confidence', 0.0):.2f}")
    print(f"  Adoptions: {network_meta.get('adoption_count', 0)}")
    print(f"  Success Rate: {network_meta.get('success_rate', 'N/A')}")
    print(f"  Created: {network_meta.get('created', 'unknown')}")
    print(f"  File: {lesson.get('_file', 'unknown')}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Review network lessons")
    parser.add_argument("--agent", default="agent", help="Current agent name")
    parser.add_argument(
        "--network-dir",
        type=Path,
        default=Path.home() / ".gptme" / "network",
        help="Network repository directory",
    )
    parser.add_argument(
        "--lessons-dir",
        type=Path,
        default=Path(__file__).parent.parent.parent / "lessons",
        help="Local lessons directory",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # List command
    list_parser = subparsers.add_parser("list", help="List network lessons")
    list_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed information"
    )

    # Show command
    show_parser = subparsers.add_parser("show", help="Show lesson details")
    show_parser.add_argument("lesson_id", help="Lesson ID to show")
    show_parser.add_argument(
        "--preview-lines", type=int, default=20, help="Number of preview lines"
    )

    # Recommend command
    rec_parser = subparsers.add_parser("recommend", help="Recommend lessons")
    rec_parser.add_argument(
        "--min-confidence", type=float, default=0.7, help="Minimum confidence threshold"
    )
    rec_parser.add_argument(
        "--min-adoption", type=int, default=0, help="Minimum adoption count"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # List lessons
    if args.command == "list":
        lessons = list_network_lessons(args.network_dir, args.agent)

        if not lessons:
            print("No lessons available from other agents")
            return 0

        print(f"Found {len(lessons)} lessons from network:\n")

        for lesson in lessons:
            print_lesson_summary(lesson)
            print()

    # Show specific lesson
    elif args.command == "show":
        lessons = list_network_lessons(args.network_dir, args.agent)

        # Find lesson by ID
        target_lesson = None
        for lesson in lessons:
            network_meta = lesson.get("network", {})
            if network_meta.get("lesson_id") == args.lesson_id:
                target_lesson = lesson
                break

        if not target_lesson:
            print(f"Lesson not found: {args.lesson_id}")
            return 1

        # Show metadata
        print_lesson_summary(target_lesson)
        print()

        # Show comparison with local
        comparison = compare_with_local(target_lesson, args.lessons_dir)
        print("Comparison:")
        print(f"  Exists locally: {comparison['exists_locally']}")
        if comparison["exists_locally"]:
            print(f"  Local path: {comparison['local_path']}")
        print(f"  Conflict: {comparison['conflict']}")
        print(f"  Recommendation: {comparison['recommendation']}")
        if "reason" in comparison:
            print(f"  Reason: {comparison['reason']}")
        print()

        # Show preview
        lesson_file = args.network_dir / target_lesson["_file"]
        print("Preview:")
        print("-" * 40)
        print(show_lesson_preview(lesson_file, args.preview_lines))

    # Recommend lessons
    elif args.command == "recommend":
        lessons = list_network_lessons(args.network_dir, args.agent)
        recommendations = recommend_lessons(
            lessons, args.lessons_dir, args.min_confidence, args.min_adoption
        )

        if not recommendations:
            print("No lessons recommended for adoption")
            return 0

        print(f"Found {len(recommendations)} recommended lessons:\n")

        for rec in recommendations:
            print_lesson_summary(rec["lesson"])
            print(f"  Recommendation: {rec['comparison']['recommendation']}")
            print(f"  Rationale: {rec['rationale']}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
