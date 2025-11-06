#!/usr/bin/env python3
"""
Monitoring CLI tool for viewing cross-platform metrics.

Usage:
    ./cli.py status [--platform PLATFORM]
    ./cli.py breakdown
    ./cli.py errors [--limit N] [--platform PLATFORM]
    ./cli.py clear
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from communication_utils.monitoring import MetricsCollector


def format_stats(stats: dict, title: str = "Statistics") -> str:
    """Format statistics dictionary for display."""
    lines = [f"\n{title}", "=" * len(title)]

    lines.append(f"Total Operations: {stats.get('total_operations', 0)}")
    lines.append(f"Completed: {stats.get('completed_operations', 0)}")
    lines.append(f"Successful: {stats.get('successful_operations', 0)}")
    lines.append(f"Failed: {stats.get('failed_operations', 0)}")
    lines.append(f"Success Rate: {stats.get('success_rate', 0.0)}%")
    lines.append(f"Avg Duration: {stats.get('avg_duration', 0.0)}s")
    lines.append(f"Error Count: {stats.get('error_count', 0)}")

    return "\n".join(lines)


def cmd_status(metrics: MetricsCollector, args) -> None:
    """Show overall or platform-specific statistics."""
    if args.platform:
        stats = metrics.get_stats(platform=args.platform)
        title = f"{args.platform.title()} Statistics"
    else:
        stats = metrics.get_stats()
        title = "Overall Statistics"

    print(format_stats(stats, title))

    # Also show per-platform breakdown if showing overall
    if not args.platform and metrics.operations:
        platforms = set(op.platform for op in metrics.operations)
        for platform in sorted(platforms):
            platform_stats = metrics.get_stats(platform=platform)
            print(format_stats(platform_stats, f"\n{platform.title()} Statistics"))


def cmd_breakdown(metrics: MetricsCollector, args) -> None:
    """Show operation breakdown by type."""
    breakdown = metrics.get_operation_breakdown()

    if not breakdown:
        print("\nNo operations recorded yet.")
        return

    print("\nOperation Breakdown")
    print("=" * 60)
    print(
        f"{'Operation':<30} {'Total':<8} {'Success':<8} {'Failed':<8} {'Avg Duration':<12}"
    )
    print("-" * 60)

    for operation, stats in sorted(breakdown.items()):
        print(
            f"{operation:<30} "
            f"{stats['total']:<8} "
            f"{stats['successful']:<8} "
            f"{stats['failed']:<8} "
            f"{stats['avg_duration']:<12.3f}s"
        )


def cmd_errors(metrics: MetricsCollector, args) -> None:
    """Show recent errors."""
    errors = metrics.get_recent_errors(limit=args.limit, platform=args.platform)

    if not errors:
        print("\nNo errors recorded.")
        return

    title = "Recent Errors"
    if args.platform:
        title += f" ({args.platform})"

    print(f"\n{title}")
    print("=" * 80)

    for i, error in enumerate(errors, 1):
        print(f"\n{i}. {error['operation']} ({error['platform']})")
        print(f"   Time: {error['timestamp']}")
        print(f"   Error: {error['error']}")


def cmd_clear(metrics: MetricsCollector, args) -> None:
    """Clear all metrics."""
    count = len(metrics.operations)
    metrics.clear()
    print(f"\nCleared {count} operation(s) from metrics.")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Monitoring CLI for viewing cross-platform metrics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Status command
    status_parser = subparsers.add_parser(
        "status", help="Show overall or platform-specific statistics"
    )
    status_parser.add_argument(
        "--platform",
        choices=["email", "twitter", "discord"],
        help="Filter by platform",
    )

    # Breakdown command
    subparsers.add_parser("breakdown", help="Show operation breakdown by type")

    # Errors command
    errors_parser = subparsers.add_parser("errors", help="Show recent errors")
    errors_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of errors to show (default: 10)",
    )
    errors_parser.add_argument(
        "--platform",
        choices=["email", "twitter", "discord"],
        help="Filter by platform",
    )

    # Clear command
    subparsers.add_parser("clear", help="Clear all metrics")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Initialize metrics collector
    # Note: In production, this would load from persistent storage
    # For now, it starts fresh each time
    metrics = MetricsCollector()

    # TODO: Load metrics from persistent storage if available
    # This could be from a JSON file, database, or shared memory

    # Execute command
    commands = {
        "status": cmd_status,
        "breakdown": cmd_breakdown,
        "errors": cmd_errors,
        "clear": cmd_clear,
    }

    commands[args.command](metrics, args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
