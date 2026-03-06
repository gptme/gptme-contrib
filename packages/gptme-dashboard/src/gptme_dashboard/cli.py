"""CLI for gptme-dashboard."""

import argparse
import sys
from pathlib import Path

from gptme_dashboard.generate import generate, generate_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a static dashboard or JSON data dump for a gptme workspace"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Path to gptme workspace (default: current directory)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output directory for generated files (default: _site for HTML, stdout for JSON)",
    )
    parser.add_argument(
        "--templates",
        type=Path,
        default=None,
        help="Custom template directory (default: built-in templates)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON data dump instead of HTML (to stdout, or data.json if --output given)",
    )
    args = parser.parse_args()

    if args.json:
        output = args.output
        json_str = generate_json(args.workspace, output)
        if output is None:
            sys.stdout.write(json_str + "\n")
    else:
        output = args.output if args.output is not None else Path("_site")
        generate(args.workspace, output, args.templates)


if __name__ == "__main__":
    main()
