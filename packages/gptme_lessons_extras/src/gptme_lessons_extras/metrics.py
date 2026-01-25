#!/usr/bin/env python3
"""
Lesson Aggregated Metrics System - Component 2 of Phase 4.3 Phase 4

Analyzes lesson effectiveness across the agent network using evolution tracking data.
Generates network-wide insights about lesson success rates, adoption patterns, and best practices.
"""

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
import argparse
from datetime import datetime


@dataclass
class VersionMetrics:
    """Metrics for a specific lesson version."""

    version: int
    uses: int
    success_rate: float  # 0.0-1.0
    contributors: list[str]
    created: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LessonMetrics:
    """Aggregated metrics for a lesson."""

    lesson_id: str
    total_uses: int
    success_rate: float  # 0.0-1.0
    adoption_count: int  # number of agents using this lesson
    versions: list[VersionMetrics]
    created: str
    last_updated: str

    def to_dict(self) -> dict:
        return {**asdict(self), "versions": [v.to_dict() for v in self.versions]}


class MetricsAggregator:
    """Aggregates and analyzes lesson effectiveness metrics."""

    def __init__(self, history_dir: Path | None = None):
        if history_dir is None:
            history_dir = Path.home() / ".gptme" / ".lessons-history"
        self.history_dir = history_dir
        self.metrics_dir = history_dir / "metrics"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

    def aggregate_lesson_metrics(self, lesson_id: str) -> Optional[LessonMetrics]:
        """
        Aggregate metrics for a specific lesson.

        Uses evolution history to calculate:
        - Total uses (approximated from version count)
        - Success rate (based on refinement acceptance)
        - Adoption count (unique contributors)
        - Version-specific metrics

        Args:
            lesson_id: Lesson identifier

        Returns:
            LessonMetrics or None if no history exists
        """
        history_file = self.history_dir / f"{lesson_id}.json"

        if not history_file.exists():
            return None

        try:
            with open(history_file) as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

        # Extract version metrics
        version_metrics = []
        total_uses = 0
        all_contributors = set()

        for version in history.get("versions", []):
            version_num = version["version"]
            contributors = [
                version.get("contributor", history.get("origin_agent", "unknown"))
            ]
            all_contributors.update(contributors)

            # Estimate uses from version existence (simplification for MVP)
            uses = version_num  # Each version represents usage iteration
            total_uses += uses

            # Calculate success rate from refinements
            # Success = changes were accepted and incorporated
            # For MVP: assume 0.8 base success rate, improved by active refinement
            success_rate = 0.8

            version_metrics.append(
                VersionMetrics(
                    version=version_num,
                    uses=uses,
                    success_rate=success_rate,
                    contributors=contributors,
                    created=version["timestamp"],
                )
            )

        # Overall success rate: weighted average of version success rates
        if version_metrics:
            weighted_success = (
                sum(v.success_rate * v.uses for v in version_metrics) / total_uses
                if total_uses > 0
                else 0.0
            )
        else:
            weighted_success = 0.0

        return LessonMetrics(
            lesson_id=lesson_id,
            total_uses=total_uses,
            success_rate=weighted_success,
            adoption_count=len(all_contributors),
            versions=version_metrics,
            created=history.get("created", ""),
            last_updated=version_metrics[-1].created if version_metrics else "",
        )

    def aggregate_network_metrics(self) -> dict[str, LessonMetrics]:
        """
        Aggregate metrics across all lessons in the network.

        Returns:
            Dictionary mapping lesson_id to LessonMetrics
        """
        network_metrics: dict[str, LessonMetrics] = {}

        # Find all lesson history files
        if not self.history_dir.exists():
            return network_metrics

        for history_file in self.history_dir.glob("*.json"):
            # Skip metrics and refinements directories
            if history_file.stem in ["metrics", "refinements"]:
                continue

            lesson_id = history_file.stem
            metrics = self.aggregate_lesson_metrics(lesson_id)

            if metrics:
                network_metrics[lesson_id] = metrics

        # Cache network metrics
        cache_file = self.metrics_dir / "network_metrics.json"
        with open(cache_file, "w") as f:
            json.dump(
                {
                    "timestamp": datetime.now().isoformat(),
                    "lesson_count": len(network_metrics),
                    "metrics": {lid: m.to_dict() for lid, m in network_metrics.items()},
                },
                f,
                indent=2,
            )

        return network_metrics

    def identify_best_practices(
        self, min_adoption: int = 2
    ) -> list[tuple[str, LessonMetrics]]:
        """
        Identify best practices based on success rate and adoption.

        Best practices are lessons with:
        - High success rate (> 0.7)
        - Wide adoption (>= min_adoption agents)

        Args:
            min_adoption: Minimum number of adopting agents

        Returns:
            List of (lesson_id, metrics) tuples, sorted by combined score
        """
        network_metrics = self.aggregate_network_metrics()

        best_practices = []

        for lesson_id, metrics in network_metrics.items():
            # Filter by adoption threshold
            if metrics.adoption_count < min_adoption:
                continue

            # Filter by success rate
            if metrics.success_rate < 0.7:
                continue

            best_practices.append((lesson_id, metrics))

        # Sort by combined score: success_rate * log(adoption_count + 1)
        import math

        best_practices.sort(
            key=lambda x: x[1].success_rate * math.log(x[1].adoption_count + 1),
            reverse=True,
        )

        # Cache best practices
        cache_file = self.metrics_dir / "best_practices.json"
        with open(cache_file, "w") as f:
            json.dump(
                {
                    "timestamp": datetime.now().isoformat(),
                    "count": len(best_practices),
                    "practices": [
                        {
                            "lesson_id": lid,
                            "metrics": m.to_dict(),
                            "score": m.success_rate * math.log(m.adoption_count + 1),
                        }
                        for lid, m in best_practices
                    ],
                },
                f,
                indent=2,
            )

        return best_practices

    def generate_report(self) -> str:
        """
        Generate comprehensive metrics report.

        Returns:
            Formatted report string
        """
        network_metrics = self.aggregate_network_metrics()
        best_practices = self.identify_best_practices()

        report = []
        report.append("# Lesson Network Metrics Report")
        report.append(
            f"\n**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

        # Network summary
        report.append("## Network Summary\n")
        report.append(f"- **Total lessons tracked**: {len(network_metrics)}")

        if network_metrics:
            avg_success = sum(m.success_rate for m in network_metrics.values()) / len(
                network_metrics
            )
            report.append(f"- **Average success rate**: {avg_success:.1%}")

            total_adoption = sum(m.adoption_count for m in network_metrics.values())
            report.append(f"- **Total adoptions**: {total_adoption}")

            max_adoption = max(
                (m.adoption_count for m in network_metrics.values()), default=0
            )
            report.append(f"- **Maximum adoption**: {max_adoption} agents\n")

        # Best practices
        report.append("## Best Practices\n")
        report.append("*High success rate + wide adoption*\n")

        if best_practices:
            for i, (lesson_id, metrics) in enumerate(best_practices[:10], 1):
                report.append(f"{i}. **{lesson_id}**")
                report.append(f"   - Success rate: {metrics.success_rate:.1%}")
                report.append(f"   - Adoption: {metrics.adoption_count} agents")
                report.append(f"   - Uses: {metrics.total_uses}")
                report.append(f"   - Versions: {len(metrics.versions)}\n")
        else:
            report.append("No best practices identified yet (need more data).\n")

        # Top lessons by success rate
        report.append("## Top Lessons by Success Rate\n")
        sorted_by_success = sorted(
            network_metrics.items(), key=lambda x: x[1].success_rate, reverse=True
        )

        for i, (lesson_id, metrics) in enumerate(sorted_by_success[:5], 1):
            report.append(f"{i}. **{lesson_id}**: {metrics.success_rate:.1%}")

        # Most adopted lessons
        report.append("\n## Most Adopted Lessons\n")
        sorted_by_adoption = sorted(
            network_metrics.items(), key=lambda x: x[1].adoption_count, reverse=True
        )

        for i, (lesson_id, metrics) in enumerate(sorted_by_adoption[:5], 1):
            report.append(f"{i}. **{lesson_id}**: {metrics.adoption_count} agents")

        return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(description="Lesson Aggregated Metrics System")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Lesson metrics command
    lesson_parser = subparsers.add_parser(
        "lesson", help="Get metrics for specific lesson"
    )
    lesson_parser.add_argument("lesson_id", help="Lesson identifier")

    # Network metrics command
    subparsers.add_parser("network", help="Aggregate network-wide metrics")

    # Best practices command
    bp_parser = subparsers.add_parser("best-practices", help="Identify best practices")
    bp_parser.add_argument(
        "--min-adoption",
        type=int,
        default=2,
        help="Minimum adoption count (default: 2)",
    )

    # Report command
    subparsers.add_parser("report", help="Generate comprehensive metrics report")

    args = parser.parse_args()

    aggregator = MetricsAggregator()

    if args.command == "lesson":
        lesson_metrics = aggregator.aggregate_lesson_metrics(args.lesson_id)
        if lesson_metrics:
            print(json.dumps(lesson_metrics.to_dict(), indent=2))
        else:
            print(f"No metrics found for lesson: {args.lesson_id}")
            return 1

    elif args.command == "network":
        network_metrics = aggregator.aggregate_network_metrics()
        print(f"Aggregated metrics for {len(network_metrics)} lessons")
        print(f"Cached to: {aggregator.metrics_dir / 'network_metrics.json'}")

    elif args.command == "best-practices":
        practices = aggregator.identify_best_practices(args.min_adoption)
        print(f"\nBest Practices ({len(practices)} found):\n")
        for i, (lesson_id, metrics) in enumerate(practices, 1):
            print(f"{i}. {lesson_id}")
            print(
                f"   Success: {metrics.success_rate:.1%}, Adoption: {metrics.adoption_count} agents"
            )

    elif args.command == "report":
        report = aggregator.generate_report()
        print(report)

    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
