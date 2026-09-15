"""
CLI for placing outbound Twilio calls into the voice server.
"""

from pathlib import Path

import click

from .twilio_integration import (
    ConfigurationError,
    build_connect_stream_twiml,
    create_outbound_call,
    outbound_identity_params,
    resolve_outbound_call_settings,
)


@click.command()
@click.argument("to_number")
@click.option(
    "--from-number",
    default=None,
    help="Twilio number to call from. Defaults to TWILIO_PHONE_NUMBER.",
)
@click.option(
    "--public-base-url",
    default=None,
    help=(
        "Public base URL for the voice server, e.g. https://example.ngrok.app. "
        "Defaults to GPTME_VOICE_PUBLIC_BASE_URL or TWILIO_PUBLIC_BASE_URL."
    ),
)
@click.option(
    "--workspace",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help=(
        "Workspace root for missed-call context persistence. "
        "Used with --context-file; defaults to the current directory."
    ),
)
@click.option(
    "--context-file",
    default=None,
    help=(
        "Workspace-relative path to a prepared context file (JSON or text). "
        "Written into the missed-call context note so a trusted callback "
        "can read it if this call goes unanswered."
    ),
)
@click.option(
    "--call-type",
    default="general",
    show_default=True,
    help="Type field for the missed-call context note (e.g. standup, general).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the generated TwiML instead of placing the call.",
)
def main(
    to_number: str,
    from_number: str | None,
    public_base_url: str | None,
    workspace: Path | None,
    context_file: str | None,
    call_type: str,
    dry_run: bool,
):
    """Place an outbound phone call that connects to the voice server."""
    try:
        settings = resolve_outbound_call_settings(
            from_number=from_number,
            public_base_url=public_base_url,
        )
    except ConfigurationError as exc:
        raise click.ClickException(str(exc)) from exc

    twiml = build_connect_stream_twiml(
        settings.stream_url,
        outbound_identity_params(to_number, settings.custom_params),
    )
    if dry_run:
        click.echo(twiml)
        return

    persist_workspace = str(workspace) if workspace is not None else None
    if context_file and persist_workspace is None:
        persist_workspace = str(Path.cwd())

    call_sid = create_outbound_call(
        to_number,
        settings,
        workspace=persist_workspace,
        context_file=context_file,
        call_type=call_type,
    )
    click.echo(f"Started call {call_sid} to {to_number} via {settings.stream_url}")
