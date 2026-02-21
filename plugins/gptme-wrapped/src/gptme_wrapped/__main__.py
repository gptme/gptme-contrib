#!/usr/bin/env python3
"""
CLI for gptme-wrapped - run analytics without loading into gptme.

Usage:
    python -m gptme_wrapped [command] [options]

Commands:
    report      Show the wrapped report (default)
    stats       Show raw statistics as JSON
    heatmap     Show activity heatmap
    export      Export to file (json/csv/html)
"""

import argparse
import json


def main():
    parser = argparse.ArgumentParser(
        description="gptme Wrapped - Year-end analytics for your gptme usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m gptme_wrapped                    # Show 2025 report
    python -m gptme_wrapped report 2024        # Show 2024 report
    python -m gptme_wrapped heatmap            # Show activity heatmap
    python -m gptme_wrapped stats              # Raw stats as JSON
    python -m gptme_wrapped export --format html > wrapped.html
        """,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="report",
        choices=["report", "stats", "heatmap", "export"],
        help="Command to run (default: report)",
    )
    parser.add_argument(
        "year",
        nargs="?",
        type=int,
        default=None,
        help="Year to analyze (default: current year)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["json", "csv", "html"],
        default="json",
        help="Export format (for export command)",
    )

    args = parser.parse_args()

    # Import here to avoid slow startup for --help
    from .tools import wrapped_export, wrapped_heatmap, wrapped_report, wrapped_stats

    if args.command == "report":
        print(wrapped_report(args.year))
    elif args.command == "heatmap":
        print(wrapped_heatmap(args.year))
    elif args.command == "stats":
        stats = wrapped_stats(args.year)
        print(json.dumps(stats, indent=2, default=str))
    elif args.command == "export":
        print(wrapped_export(args.year, format=args.format))


if __name__ == "__main__":
    main()
