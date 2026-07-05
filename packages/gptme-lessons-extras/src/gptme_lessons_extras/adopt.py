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
    import click

    @click.group()
    @click.option("--agent", default="agent", help="Current agent name")
    @click.option(
        "--network-dir",
        type=click.Path(),
        default=str(Path.home() / ".gptme" / "network"),
        help="Network repository directory",
    )
    @click.option(
        "--lessons-dir",
        type=click.Path(),
        default=str(Path(__file__).parent.parent.parent / "lessons"),
        help="Local lessons directory",
    )
    @click.pass_context
    def cli(ctx, agent, network_dir, lessons_dir):
        """Adopt network lessons."""
        ctx.ensure_object(dict)
        ctx.obj["agent"] = agent
        ctx.obj["network_dir"] = Path(network_dir)
        ctx.obj["lessons_dir"] = Path(lessons_dir)

    @cli.command()
    @click.argument("lesson_id")
    @click.option("--force", is_flag=True, help="Overwrite if local lesson exists")
    @click.pass_context
    def adopt(ctx, lesson_id, force):
        """Adopt a specific lesson."""
        network_dir = ctx.obj["network_dir"]
        lessons_dir = ctx.obj["lessons_dir"]
        agent = ctx.obj["agent"]

        # Get lesson metadata
        lessons = review_module.list_network_lessons(network_dir, agent)

        target_lesson = None
        for lesson in lessons:
            network_meta = lesson.get("network", {})
            if network_meta.get("lesson_id") == lesson_id:
                target_lesson = lesson
                break

        if not target_lesson:
            print(f"Lesson not found: {lesson_id}")
            sys.exit(1)

        success, message = adopt_lesson(
            lesson_metadata=target_lesson,
            network_dir=network_dir,
            lessons_dir=lessons_dir,
            force=force,
        )

        if success:
            print(f"✓ {message}")
        else:
            print(f"✗ {message}")
            sys.exit(1)

    @cli.command()
    @click.option(
        "--min-confidence", type=float, default=0.7, help="Minimum confidence threshold"
    )
    @click.option("--min-adoption", type=int, default=0, help="Minimum adoption count")
    @click.option("--force", is_flag=True, help="Overwrite existing lessons")
    @click.option(
        "--auto-confirm", "-y", is_flag=True, help="Adopt all without confirmation"
    )
    @click.pass_context
    def batch(ctx, min_confidence, min_adoption, force, auto_confirm):
        """Adopt recommended lessons."""
        network_dir = ctx.obj["network_dir"]
        lessons_dir = ctx.obj["lessons_dir"]
        agent = ctx.obj["agent"]

        # Get recommendations
        lessons = review_module.list_network_lessons(network_dir, agent)
        recommendations = review_module.recommend_lessons(
            lessons, lessons_dir, min_confidence, min_adoption
        )

        if not recommendations:
            print("No lessons recommended for adoption")
            return

        print(f"Found {len(recommendations)} recommended lessons\n")

        results = batch_adopt(
            recommendations=recommendations,
            network_dir=network_dir,
            lessons_dir=lessons_dir,
            force=force,
            auto_confirm=auto_confirm,
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

    @cli.command("metrics")
    @click.pass_context
    def metrics_cmd(ctx):
        """Show adoption metrics."""
        lessons_dir = ctx.obj["lessons_dir"]
        metrics = report_adoption_metrics(lessons_dir)

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

    cli()


if __name__ == "__main__":
    main()
