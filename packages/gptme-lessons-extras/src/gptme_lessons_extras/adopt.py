#!/usr/bin/env python3
"""
Lesson adoption workflow for agent network protocol.

Provides tools to:
- Adopt lessons from network
- Resolve conflicts with local lessons
- Track adoption metrics
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import from existing modules
try:
    from lessons import review as review_module
except ImportError:
    # Allow running from package directory
    import review as review_module  # type: ignore[import-not-found, no-redef]


def adopt_lesson(
    lesson_metadata: dict[str, Any],
    network_dir: Path,
    lessons_dir: Path,
    force: bool = False,
    increment_adoption: bool = True,
) -> tuple[bool, str]:
    """
    Adopt a lesson from the network into local lessons directory.

    Args:
        lesson_metadata: Network lesson metadata dict
        network_dir: Network repository directory
        lessons_dir: Local lessons directory
        force: Overwrite if local lesson exists
        increment_adoption: Increment adoption_count in network

    Returns:
        (success, message) tuple
    """
    try:
        # Extract lesson info
        network_meta = lesson_metadata.get("network", {})
        lesson_id = network_meta.get("lesson_id")
        if not lesson_id:
            return False, "Missing lesson_id in metadata"

        # Get source file
        lesson_file = network_dir / lesson_metadata["_file"]
        if not lesson_file.exists():
            return False, f"Source file not found: {lesson_file}"

        # Determine local path from lesson_id
        # Format: agent-category-slug
        parts = lesson_id.split("-", 2)
        if len(parts) < 3:
            return False, f"Invalid lesson_id format: {lesson_id}"

        _, category, slug = parts
        local_path = lessons_dir / category / f"{slug}.md"

        # Check for conflicts
        if local_path.exists() and not force:
            return (
                False,
                f"Local lesson exists: {local_path}. Use --force to overwrite.",
            )

        # Create category directory if needed
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Read source content
        with open(lesson_file) as f:
            content = f.read()

        # Parse frontmatter to update adoption_count
        if increment_adoption and content.startswith("---\n"):
            end_idx = content.find("\n---\n", 4)
            if end_idx != -1:
                import yaml

                frontmatter_str = content[4:end_idx]
                metadata = yaml.safe_load(frontmatter_str)

                # Increment adoption count
                if "network" in metadata:
                    current_count = metadata["network"].get("adoption_count", 0)
                    metadata["network"]["adoption_count"] = current_count + 1
                    metadata["network"]["updated"] = datetime.now(
                        timezone.utc
                    ).isoformat()

                    # Reconstruct content with updated metadata
                    import io

                    buf = io.StringIO()
                    yaml.safe_dump(metadata, buf, default_flow_style=False)
                    new_frontmatter = buf.getvalue()

                    content = f"---\n{new_frontmatter}---\n{content[end_idx + 5 :]}"

        # Write to local path
        with open(local_path, "w") as f:
            f.write(content)

        # Record adoption in metrics (for future tracking)
        record_adoption(
            lesson_id=lesson_id,
            agent_origin=network_meta.get("agent_origin", "unknown"),
            adopted_at=datetime.now(timezone.utc).isoformat(),
            lessons_dir=lessons_dir,
        )

        return True, f"Adopted to {local_path}"

    except Exception as e:
        return False, f"Error adopting lesson: {e}"


def record_adoption(
    lesson_id: str, agent_origin: str, adopted_at: str, lessons_dir: Path
) -> None:
    """
    Record lesson adoption for metrics tracking.

    Args:
        lesson_id: Lesson ID that was adopted
        agent_origin: Agent that created the lesson
        adopted_at: ISO timestamp of adoption
        lessons_dir: Local lessons directory
    """
    metrics_file = lessons_dir.parent / "knowledge" / "meta" / "lesson-adoptions.json"
    metrics_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing metrics
    if metrics_file.exists():
        with open(metrics_file) as f:
            metrics = json.load(f)
    else:
        metrics = {"adoptions": []}

    # Add new adoption
    metrics["adoptions"].append(
        {"lesson_id": lesson_id, "agent_origin": agent_origin, "adopted_at": adopted_at}
    )

    # Save metrics
    with open(metrics_file, "w") as f:
        json.dump(metrics, f, indent=2)


def batch_adopt(
    recommendations: list[dict[str, Any]],
    network_dir: Path,
    lessons_dir: Path,
    force: bool = False,
    auto_confirm: bool = False,
) -> dict[str, Any]:
    """
    Adopt multiple recommended lessons in batch.

    Args:
        recommendations: List of recommendation dicts from review.recommend_lessons
        network_dir: Network repository directory
        lessons_dir: Local lessons directory
        force: Overwrite existing lessons
        auto_confirm: Don't prompt for confirmation

    Returns:
        Result dict with counts and errors
    """
    results: dict[str, Any] = {"adopted": [], "skipped": [], "errors": []}

    for rec in recommendations:
        lesson = rec["lesson"]
        network_meta = lesson.get("network", {})
        lesson_id = network_meta.get("lesson_id", "unknown")

        # Check if manual review needed
        if rec["comparison"]["recommendation"] == "review" and not auto_confirm:
            print(f"\n{lesson_id}:")
            print(f"  Rationale: {rec['rationale']}")
            response = input("  Adopt this lesson? [y/N]: ").strip().lower()
            if response not in ("y", "yes"):
                results["skipped"].append(lesson_id)
                continue

        # Attempt adoption
        success, message = adopt_lesson(
            lesson_metadata=lesson,
            network_dir=network_dir,
            lessons_dir=lessons_dir,
            force=force,
        )

        if success:
            results["adopted"].append(lesson_id)
            print(f"✓ Adopted: {lesson_id}")
        else:
            results["errors"].append({"lesson_id": lesson_id, "error": message})
            print(f"✗ Failed: {lesson_id} - {message}")

    return results


def report_adoption_metrics(lessons_dir: Path) -> dict[str, Any]:
    """
    Generate report of adoption metrics.

    Args:
        lessons_dir: Local lessons directory

    Returns:
        Metrics dict
    """
    metrics_file = lessons_dir.parent / "knowledge" / "meta" / "lesson-adoptions.json"

    if not metrics_file.exists():
        return {"total_adoptions": 0, "by_agent": {}, "recent": []}

    with open(metrics_file) as f:
        data = json.load(f)

    adoptions = data.get("adoptions", [])

    # Count by agent
    by_agent: dict[str, int] = {}
    for adoption in adoptions:
        agent = adoption["agent_origin"]
        by_agent[agent] = by_agent.get(agent, 0) + 1

    # Recent adoptions (last 10)
    recent = sorted(adoptions, key=lambda x: x["adopted_at"], reverse=True)[:10]

    return {"total_adoptions": len(adoptions), "by_agent": by_agent, "recent": recent}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Adopt network lessons")
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

    # Adopt single lesson
    adopt_parser = subparsers.add_parser("adopt", help="Adopt a specific lesson")
    adopt_parser.add_argument("lesson_id", help="Lesson ID to adopt")
    adopt_parser.add_argument(
        "--force", action="store_true", help="Overwrite if local lesson exists"
    )

    # Batch adopt recommendations
    batch_parser = subparsers.add_parser("batch", help="Adopt recommended lessons")
    batch_parser.add_argument(
        "--min-confidence", type=float, default=0.7, help="Minimum confidence threshold"
    )
    batch_parser.add_argument(
        "--min-adoption", type=int, default=0, help="Minimum adoption count"
    )
    batch_parser.add_argument(
        "--force", action="store_true", help="Overwrite existing lessons"
    )
    batch_parser.add_argument(
        "--auto-confirm",
        "-y",
        action="store_true",
        help="Adopt all without confirmation",
    )

    # Show metrics
    subparsers.add_parser("metrics", help="Show adoption metrics")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Adopt specific lesson
    if args.command == "adopt":
        # Get lesson metadata
        lessons = review_module.list_network_lessons(args.network_dir, args.agent)

        target_lesson = None
        for lesson in lessons:
            network_meta = lesson.get("network", {})
            if network_meta.get("lesson_id") == args.lesson_id:
                target_lesson = lesson
                break

        if not target_lesson:
            print(f"Lesson not found: {args.lesson_id}")
            return 1

        success, message = adopt_lesson(
            lesson_metadata=target_lesson,
            network_dir=args.network_dir,
            lessons_dir=args.lessons_dir,
            force=args.force,
        )

        if success:
            print(f"✓ {message}")
            return 0
        else:
            print(f"✗ {message}")
            return 1

    # Batch adopt recommendations
    elif args.command == "batch":
        # Get recommendations
        lessons = review_module.list_network_lessons(args.network_dir, args.agent)
        recommendations = review_module.recommend_lessons(
            lessons, args.lessons_dir, args.min_confidence, args.min_adoption
        )

        if not recommendations:
            print("No lessons recommended for adoption")
            return 0

        print(f"Found {len(recommendations)} recommended lessons\n")

        results = batch_adopt(
            recommendations=recommendations,
            network_dir=args.network_dir,
            lessons_dir=args.lessons_dir,
            force=args.force,
            auto_confirm=args.auto_confirm,
        )

        # Print summary
        print("\nAdoption Summary:")
        print(f"  Adopted: {len(results['adopted'])}")
        print(f"  Skipped: {len(results['skipped'])}")
        print(f"  Errors: {len(results['errors'])}")

        if results["errors"]:
            print("\nErrors:")
            for error in results["errors"]:
                print(f"  {error['lesson_id']}: {error['error']}")

    # Show metrics
    elif args.command == "metrics":
        metrics = report_adoption_metrics(args.lessons_dir)

        print("Adoption Metrics:\n")
        print(f"Total adoptions: {metrics['total_adoptions']}")

        if metrics["by_agent"]:
            print("\nBy agent:")
            for agent, count in sorted(
                metrics["by_agent"].items(), key=lambda x: x[1], reverse=True
            ):
                print(f"  {agent}: {count}")

        if metrics["recent"]:
            print("\nRecent adoptions:")
            for adoption in metrics["recent"]:
                print(f"  {adoption['lesson_id']} from {adoption['agent_origin']}")
                print(f"    at {adoption['adopted_at']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
