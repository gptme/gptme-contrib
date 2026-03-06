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

import json

import click


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """gptme Wrapped - Year-end analytics for your gptme usage."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(report)


@main.command()
@click.argument("year", type=int, required=False, default=None)
def report(year: int | None) -> None:
    """Show the wrapped report."""
    from .tools import wrapped_report

    print(wrapped_report(year))


@main.command()
@click.argument("year", type=int, required=False, default=None)
def stats(year: int | None) -> None:
    """Show raw statistics as JSON."""
    from .tools import wrapped_stats

    result = wrapped_stats(year)
    print(json.dumps(result, indent=2, default=str))


@main.command()
@click.argument("year", type=int, required=False, default=None)
def heatmap(year: int | None) -> None:
    """Show activity heatmap."""
    from .tools import wrapped_heatmap

    print(wrapped_heatmap(year))


@main.command()
@click.argument("year", type=int, required=False, default=None)
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["json", "csv", "html"]),
    default="json",
    help="Export format",
)
def export(year: int | None, fmt: str) -> None:
    """Export to file (json/csv/html)."""
    from .tools import wrapped_export

    print(wrapped_export(year, format=fmt))


if __name__ == "__main__":
    main()
