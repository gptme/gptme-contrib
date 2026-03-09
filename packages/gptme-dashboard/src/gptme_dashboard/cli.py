"""CLI for gptme-dashboard."""

import sys
from pathlib import Path

import click

from gptme_dashboard.generate import collect_workspace_data, generate, generate_json


@click.command()
@click.option(
    "--workspace",
    type=click.Path(),
    default=".",
    show_default=True,
    help="Path to gptme workspace.",
)
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Output directory (default: _site). Generates both index.html and data.json.",
)
@click.option(
    "--templates",
    type=click.Path(),
    default=None,
    help="Custom Jinja2 template directory.",
)
@click.option(
    "--json",
    "print_json",
    is_flag=True,
    default=False,
    help="Print JSON data dump to stdout. Without --output, skips HTML generation.",
)
def main(workspace: str, output: str | None, templates: str | None, print_json: bool) -> None:
    """Generate a static dashboard and JSON data dump for a gptme workspace."""
    ws = Path(workspace)
    tmpl = Path(templates) if templates is not None else None

    if print_json and output is None:
        # Stdout-only JSON mode (for piping to jq, CI artifacts, etc.)
        click.echo(generate_json(ws))
        return

    out = Path(output) if output is not None else Path("_site")
    data = collect_workspace_data(ws)
    generate(ws, out, tmpl, data=data)
    json_str = generate_json(ws, out, data=data)

    if print_json:
        sys.stdout.write(json_str + "\n")


if __name__ == "__main__":
    main()
