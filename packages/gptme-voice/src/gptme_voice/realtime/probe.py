"""
Live preflight probe for a realtime voice provider.

Connects to the provider, sends the *same* ``session.update`` the voice server
would send (built by :class:`VoiceServer` from the workspace's project
instructions, provider, model, voice and env-driven options), and waits for
the provider to either apply it (``session.updated``) or reject it
(``error``). No audio is sent and no response is requested, so a probe costs
one websocket handshake.

Intended as a gate before flipping a production voice deployment to a new
provider/model: a schema rejection exits non-zero instead of surfacing as an
unconfigured live call.

Usage::

    python -m gptme_voice.realtime.probe --provider openai --model gpt-realtime-2 --voice cedar
    python -m gptme_voice.realtime.probe --provider grok

Exit codes: 0 = session configured, 1 = rejected / timed out / connect failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import click

from .server import (
    _PROVIDER_OPENAI,
    _VALID_PROVIDERS,
    _VALID_REASONING_EFFORTS,
    VoiceServer,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 10.0
# After session.updated, linger briefly so a trailing error (a provider that
# applies part of the config and rejects the rest) still fails the probe.
_DEFAULT_SETTLE_SECONDS = 1.0


@dataclass
class ProbeResult:
    ok: bool
    provider: str
    model: str
    elapsed_seconds: float
    error: Any = None
    requested: dict[str, Any] = field(default_factory=dict)
    applied: dict[str, Any] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "error": self.error,
            "requested": self.requested,
            "applied": self.applied,
            "mismatches": self.mismatches,
        }


def _tool_name(tool: dict[str, Any]) -> str:
    """Tool name from either the flat (OpenAI) or nested (xAI echo) shape."""
    name = tool.get("name")
    if not name and isinstance(tool.get("function"), dict):
        name = tool["function"].get("name")
    return str(name or "?")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _summarize_session(session: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields that matter for a voice call out of either shape."""
    audio = _as_dict(session.get("audio"))
    audio_in = _as_dict(audio.get("input"))
    audio_out = _as_dict(audio.get("output"))
    tools = session.get("tools")
    instructions = session.get("instructions")
    turn_detection = audio_in.get("turn_detection") or session.get("turn_detection")
    summary: dict[str, Any] = {
        "type": session.get("type"),
        "model": session.get("model"),
        "output_modalities": session.get("output_modalities")
        or session.get("modalities"),
        "voice": audio_out.get("voice") or session.get("voice"),
        "input_format": audio_in.get("format") or session.get("input_audio_format"),
        "output_format": audio_out.get("format") or session.get("output_audio_format"),
        "speed": audio_out.get("speed")
        or _as_dict(session.get("output")).get("speed")
        or session.get("speed"),
        "transcription": audio_in.get("transcription")
        or session.get("input_audio_transcription"),
        "turn_detection": turn_detection.get("type")
        if isinstance(turn_detection, dict)
        else None,
        "reasoning": session.get("reasoning"),
        "instructions_chars": len(instructions)
        if isinstance(instructions, str)
        else None,
        "tools": sorted(_tool_name(t) for t in tools if isinstance(t, dict))
        if isinstance(tools, list)
        else None,
    }
    return {k: v for k, v in summary.items() if v is not None}


def _compare(requested: dict[str, Any], applied: dict[str, Any]) -> list[str]:
    """Flag config the provider accepted but did not apply.

    Only compares fields the provider echoes back; absent fields are not
    treated as mismatches (providers differ in how much they echo).
    """
    mismatches: list[str] = []
    for key in ("voice", "tools"):
        if key in applied and key in requested and applied[key] != requested[key]:
            mismatches.append(
                f"{key}: requested {requested[key]!r}, applied {applied[key]!r}"
            )
    if (
        applied.get("instructions_chars") == 0
        and requested.get("instructions_chars", 0) > 0
    ):
        mismatches.append("instructions: requested non-empty, applied empty")
    return mismatches


async def run_probe(
    server: VoiceServer,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    settle_seconds: float = _DEFAULT_SETTLE_SECONDS,
) -> ProbeResult:
    """Send the server's real session.update and report whether it applied."""
    session_cfg = server._build_session_config(server._instructions)
    client = server._make_client(session_cfg)
    requested = _summarize_session(
        client._build_session_params(
            session_cfg.instructions or "", client._build_tools()
        )
    )

    errors: list[Any] = []
    applied: dict[str, Any] = {}
    settled = asyncio.Event()
    original_handle_event = client._handle_event

    async def _tap(event: dict) -> None:
        event_type = event.get("type")
        if event_type == "error":
            errors.append(event.get("error"))
            settled.set()
        elif event_type == "session.updated":
            session = event.get("session")
            if isinstance(session, dict):
                applied.update(_summarize_session(session))
            settled.set()
        await original_handle_event(event)

    # Instance attribute shadows the method, so the receive loop sees the tap.
    client._handle_event = _tap  # type: ignore[method-assign]

    start = time.monotonic()
    try:
        await client.connect()
        try:
            await asyncio.wait_for(settled.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            errors.append(f"no session.updated or error within {timeout_seconds:.1f}s")
        if not errors and settle_seconds > 0:
            await asyncio.sleep(settle_seconds)
    except Exception as exc:  # connect/handshake failure
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        elapsed = time.monotonic() - start
        try:
            await client.disconnect()
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.debug("probe disconnect failed: %s", exc)

    mismatches = _compare(requested, applied) if not errors else []
    error: Any = None
    if errors:
        error = errors[0] if len(errors) == 1 else errors
    return ProbeResult(
        ok=not errors and not mismatches,
        provider=server.provider,
        model=client.session_config.model,
        elapsed_seconds=elapsed,
        error=error,
        requested=requested,
        applied=applied,
        mismatches=mismatches,
    )


@click.command()
@click.option("--workspace", default=None, help="Agent workspace (auto-detected).")
@click.option(
    "--provider",
    default=_PROVIDER_OPENAI,
    type=click.Choice(_VALID_PROVIDERS),
    show_default=True,
)
@click.option("--model", default=None, help="Realtime model override.")
@click.option(
    "--reasoning-effort",
    default="low",
    type=click.Choice(_VALID_REASONING_EFFORTS),
    show_default=True,
    help="Same default as the voice server. Ignored for xAI.",
)
@click.option("--voice", default=None, help="Provider voice override.")
@click.option("--output-speed", default=None, type=click.FloatRange(0.25, 1.5))
@click.option(
    "--timeout",
    "timeout_seconds",
    default=_DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    type=float,
    help="Seconds to wait for session.updated / error.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
@click.option("--debug", is_flag=True, help="Enable debug logging.")
def main(
    workspace: str | None,
    provider: str,
    model: str | None,
    reasoning_effort: str,
    voice: str | None,
    output_speed: float | None,
    timeout_seconds: float,
    as_json: bool,
    debug: bool,
) -> None:
    """Probe that a provider accepts the voice server's session.update."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # websockets debug logging leaks the API key in headers.
    logging.getLogger("websockets").setLevel(logging.WARNING)

    try:
        server = VoiceServer(
            workspace=workspace,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            voice=voice,
            output_speed=output_speed,
        )
        result = asyncio.run(run_probe(server, timeout_seconds=timeout_seconds))
    except Exception as exc:
        click.echo(f"FAIL provider={provider}: {type(exc).__name__}: {exc}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        status = "OK" if result.ok else "FAIL"
        click.echo(
            f"{status} provider={result.provider} model={result.model} "
            f"({result.elapsed_seconds:.2f}s)"
        )
        if result.error is not None:
            click.echo(f"  error: {result.error}")
        for mismatch in result.mismatches:
            click.echo(f"  mismatch: {mismatch}")
        click.echo(f"  requested: {json.dumps(result.requested, default=str)}")
        if result.applied:
            click.echo(f"  applied:   {json.dumps(result.applied, default=str)}")
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
