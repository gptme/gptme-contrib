"""CLI for gptme-dashboard."""

import sys
from pathlib import Path

import click


class DefaultGroup(click.Group):
    """Click group that defaults to 'generate' when no subcommand is given.

    This preserves backward compatibility: ``gptme-dashboard --workspace .``
    still works and maps to ``gptme-dashboard generate --workspace .``.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # If no args or first arg looks like an option (not a subcommand), prepend 'generate'
        if not args or (args[0].startswith("-") and args[0] not in ("--help", "-h")):
            args = ["generate"] + list(args)
        elif args[0] not in self.commands and args[0] not in ("--help", "-h"):
            args = ["generate"] + list(args)
        return super().parse_args(ctx, args)  # type: ignore[no-any-return]


@click.group(cls=DefaultGroup)
def main() -> None:
    """Dashboard generator and server for gptme workspaces."""


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True),
    default=".",
    show_default=True,
    help="Path to gptme workspace.",
)
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Output directory (default: <workspace>/_site). Generates both index.html and data.json.",
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
@click.option(
    "--sessions/--no-sessions",
    default=False,
    show_default=True,
    help=(
        "Scan recent agent sessions via gptme-sessions and include them in the "
        "dashboard.  Requires the gptme-sessions package to be installed."
    ),
)
@click.option(
    "--sessions-days",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Number of days back to scan for sessions (used with --sessions).",
)
@click.option(
    "--base-url",
    default="",
    help=(
        "Base URL for sitemap.xml generation (e.g. https://owner.github.io/repo/). "
        "Auto-detected from GitHub remote when omitted. "
        "Pass '-' to suppress sitemap generation."
    ),
)
def generate(
    workspace: str,
    output: str | None,
    templates: str | None,
    print_json: bool,
    sessions: bool,
    sessions_days: int,
    base_url: str,
) -> None:
    """Generate a static dashboard and JSON data dump for a gptme workspace."""
    from gptme_dashboard.generate import generate as do_generate
    from gptme_dashboard.generate import generate_json

    ws = Path(workspace)
    tmpl = Path(templates) if templates is not None else None

    if print_json and output is None:
        # Stdout-only JSON mode (for piping to jq, CI artifacts, etc.)
        click.echo(generate_json(ws, include_sessions=sessions, sessions_days=sessions_days))
        return

    out = Path(output) if output is not None else ws / "_site"
    data = do_generate(
        ws, out, tmpl, include_sessions=sessions, sessions_days=sessions_days, base_url=base_url
    )
    json_str = generate_json(ws, out, _data=data)

    if print_json:
        sys.stdout.write(json_str + "\n")


@main.command()
@click.option(
    "--workspace",
    type=click.Path(exists=True),
    default=".",
    show_default=True,
    help="Path to gptme workspace.",
)
@click.option(
    "--port",
    type=int,
    default=8042,
    show_default=True,
    help="Port to serve on.",
)
@click.option(
    "--host",
    type=str,
    default="127.0.0.1",
    show_default=True,
    help="Host to bind to.",
)
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Static site directory (default: <workspace>/_site).",
)
@click.option(
    "--org",
    "org_config",
    type=click.Path(exists=True),
    default=None,
    help=(
        "Path to org TOML config listing remote agent API endpoints. "
        "Enables /api/org aggregation endpoint and /org view page. "
        "Example: ~/.config/gptme/org.toml"
    ),
)
def serve(workspace: str, port: int, host: str, output: str | None, org_config: str | None) -> None:
    """Serve the dashboard with live API endpoints.

    Generates the static site and serves it alongside API endpoints
    for session stats and agent status. Requires Flask:
    ``pip install gptme-dashboard[serve]``

    Pass --org <org.toml> to also enable the org view (agent grid) at /org.
    """
    from gptme_dashboard.server import create_app

    ws = Path(workspace)
    site = Path(output) if output else None
    org = Path(org_config) if org_config else None

    app = create_app(ws, site_dir=site, org_config=org)
    click.echo(f"Serving dashboard at http://{host}:{port}")
    click.echo(f"  Workspace: {ws.resolve()}")
    click.echo(f"  API: http://{host}:{port}/api/status")
    if org:
        click.echo(f"  Org view: http://{host}:{port}/org")
        click.echo(f"  Org API:  http://{host}:{port}/api/org")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
