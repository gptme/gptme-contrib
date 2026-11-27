"""CLI for gptme-dashboard."""

import json
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
@click.option(
    "--include-terminal-tasks",
    is_flag=True,
    default=False,
    help=(
        "Include done/cancelled tasks in the output. "
        "By default they are omitted to keep the initial payload small; "
        "their detail pages are always generated."
    ),
)
@click.option(
    "--codesize", "include_codesize", is_flag=True, help="Include the code-size section and trend."
)
@click.option(
    "--dependent-repo",
    "dependent_repos",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Count declared dependents in this agent checkout (repeatable, requires --codesize).",
)
@click.option(
    "--ci-packages",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JSON array of package directories selected by CI (requires --codesize).",
)
def generate(
    workspace: str,
    output: str | None,
    templates: str | None,
    print_json: bool,
    sessions: bool,
    sessions_days: int,
    base_url: str,
    include_terminal_tasks: bool,
    include_codesize: bool,
    dependent_repos: tuple[Path, ...],
    ci_packages: Path | None,
) -> None:
    """Generate a static dashboard and JSON data dump for a gptme workspace."""
    from gptme_dashboard.generate import generate as do_generate
    from gptme_dashboard.generate import generate_json

    ws = Path(workspace)
    tmpl = Path(templates) if templates is not None else None
    omit_terminal = not include_terminal_tasks

    if not include_codesize and (dependent_repos or ci_packages is not None):
        raise click.UsageError("--dependent-repo and --ci-packages require --codesize")
    codesize_data = None
    if include_codesize:
        from gptme_dashboard.codesize_section import collect_codesize_section

        matrix = None
        if ci_packages is not None:
            try:
                names = json.loads(ci_packages.read_text(encoding="utf-8"))
                if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                    raise ValueError("expected an array of package directory names")
                matrix = set(names)
            except (OSError, ValueError) as exc:
                raise click.BadParameter(str(exc), param_hint="--ci-packages") from exc
        try:
            codesize_data = collect_codesize_section(
                ws, agent_repos=dependent_repos, ci_packages=matrix
            )
        except (OSError, ValueError) as exc:
            raise click.ClickException(f"Code-size section: {exc}") from exc

    if print_json and output is None:
        # Stdout-only JSON mode (for piping to jq, CI artifacts, etc.)
        click.echo(
            generate_json(
                ws,
                include_sessions=sessions,
                sessions_days=sessions_days,
                omit_terminal_tasks=omit_terminal,
                codesize=codesize_data,
            )
        )
        return

    out = Path(output) if output is not None else ws / "_site"
    data = do_generate(
        ws,
        out,
        tmpl,
        include_sessions=sessions,
        sessions_days=sessions_days,
        base_url=base_url,
        omit_terminal_tasks=omit_terminal,
        codesize=codesize_data,
    )
    json_str = generate_json(ws, out, _data=data)

    if print_json:
        sys.stdout.write(json_str + "\n")


@main.command()
@click.option(
    "--repo",
    "repo",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Repository (or workspace) to report on.",
)
@click.option(
    "--json",
    "print_json",
    is_flag=True,
    default=False,
    help="Print the report as JSON instead of markdown.",
)
@click.option(
    "--groups",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "JSON file mapping path prefixes to group names, e.g. "
        '{"src/core": "core", "src/llm": "llm"}. Defaults to '
        "<repo>/codesize-groups.json when present."
    ),
)
@click.option(
    "--threshold",
    "thresholds",
    multiple=True,
    type=int,
    help=(
        "File-size threshold in lines to flag (repeatable). Default: 1500 and 3000, "
        "plus the threshold in <repo>/file-size-baseline.json when present."
    ),
)
@click.option(
    "--history-weeks",
    type=click.IntRange(min=0, max=104),
    default=8,
    show_default=True,
    help="Weekly git samples for the trend. 0 disables the history scan.",
)
@click.option(
    "--largest",
    type=click.IntRange(min=1),
    default=15,
    show_default=True,
    help="How many of the largest files and hotspots to list.",
)
@click.option("--code-only", is_flag=True, help="Exclude prose and configuration files.")
def codesize(
    repo: str,
    print_json: bool,
    groups: str | None,
    thresholds: tuple[int, ...],
    history_weeks: int,
    largest: int,
    code_only: bool,
) -> None:
    """Report where code is hard to understand, review or change.

    Leads with the largest source files, then over-threshold files, hotspots
    (size x churn), a per-group breakdown and a weekly trend sampled from git.
    """
    from gptme_dashboard.codesize import (
        DEFAULT_THRESHOLDS,
        build_report,
        parse_group_map,
        render_markdown,
    )

    group_map = None
    if groups is not None:
        try:
            group_map = parse_group_map(json.loads(Path(groups).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise click.BadParameter(str(exc), param_hint="'--groups'") from exc

    report = build_report(
        Path(repo),
        group_map=group_map,
        thresholds=thresholds or DEFAULT_THRESHOLDS,
        history_weeks=history_weeks,
        largest_n=largest,
        code_only=code_only,
    )
    click.echo(report.to_json() if print_json else render_markdown(report))


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
def serve(
    workspace: str,
    port: int,
    host: str,
    output: str | None,
    org_config: str | None,
) -> None:
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
