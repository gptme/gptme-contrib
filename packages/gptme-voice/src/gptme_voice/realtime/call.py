"""
CLI for placing outbound Twilio calls into the voice server.
"""

import click

from .twilio_integration import (
    ConfigurationError,
    build_connect_stream_twiml,
    create_outbound_call,
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
    "--dry-run",
    is_flag=True,
    help="Print the generated TwiML instead of placing the call.",
)
def main(
    to_number: str,
    from_number: str | None,
    public_base_url: str | None,
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

    twiml = build_connect_stream_twiml(settings.stream_url)
    if dry_run:
        click.echo(twiml)
        return

    call_sid = create_outbound_call(to_number, settings)
    click.echo(f"Started call {call_sid} to {to_number} via {settings.stream_url}")
