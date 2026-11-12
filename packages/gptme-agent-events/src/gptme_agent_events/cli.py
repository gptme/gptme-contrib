"""Command line: ``gptme-agent-events tail`` produces the stream, sinks consume it.

# on the agent's host: one JSON event per line on stdout
gptme-agent-events tail --agent bob

# anywhere with speakers: play it
ssh bob 'gptme-agent-events tail --agent bob' | gptme-agent-events sound
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import click

from .event import AgentEvent
from .follow import HARNESSES, SOURCE_FACTORIES, Follower, Source
from .sound import DEFAULT_SOUNDS, SoundPlayer, find_sounds_dir


def _default_agent() -> str:
    return os.environ.get("AGENT_NAME") or socket.gethostname().split(".")[0]


@click.group()
def main() -> None:
    """Live event stream of what agents are doing, plus sinks for it."""


@main.command()
@click.option("--agent", default=_default_agent, help="Agent name stamped on every event.")
@click.option(
    "--harness",
    "harnesses",
    multiple=True,
    type=click.Choice(HARNESSES),
    help="Only follow these harnesses (default: all).",
)
@click.option(
    "--root",
    "roots",
    multiple=True,
    metavar="HARNESS=DIR",
    help="Extra or replacement trajectory root, e.g. gptme=~/bob/logs. Repeatable.",
)
@click.option("--from-start", is_flag=True, help="Replay recently active files first.")
@click.option("--once", is_flag=True, help="Do one pass and exit (with --from-start: replay).")
@click.option("--active-window", default=600.0, show_default=True, help="Seconds of idleness.")
@click.option("--interval", default=0.5, show_default=True, help="Poll interval in seconds.")
def tail(
    agent: str,
    harnesses: tuple[str, ...],
    roots: tuple[str, ...],
    from_start: bool,
    once: bool,
    active_window: float,
    interval: float,
) -> None:
    """Follow live trajectories on this host and print events as JSON lines."""
    selected = harnesses or HARNESSES
    custom: dict[str, list[Path]] = {}
    for spec in roots:
        harness, sep, directory = spec.partition("=")
        if not sep or harness not in SOURCE_FACTORIES:
            raise click.BadParameter(f"expected HARNESS=DIR with HARNESS in {HARNESSES}: {spec}")
        custom.setdefault(harness, []).append(Path(directory).expanduser())
    sources: list[Source] = []
    for harness in selected:
        factory = SOURCE_FACTORIES[harness]
        dirs: list[Path | None] = list(custom.get(harness, [])) or [None]
        sources += [factory(d) for d in dirs]

    follower = Follower(sources, agent, active_window=active_window, from_start=from_start)
    try:
        if once:
            for ev in follower.poll():
                click.echo(ev.to_json())
            return
        for ev in follower.run(interval):
            # Flush per event: consumers are usually on the far end of a pipe or ssh.
            click.echo(ev.to_json())
            sys.stdout.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass


def _pairs(values: tuple[str, ...], what: str) -> dict[str, str]:
    out = {}
    for v in values:
        key, sep, val = v.partition("=")
        if not sep:
            raise click.BadParameter(f"expected KEY=VALUE for {what}: {v}")
        out[key] = val
    return out


@main.command()
@click.option("--volume", default=0.5, show_default=True, type=click.FloatRange(0, 1))
@click.option(
    "--agent-volume",
    multiple=True,
    metavar="AGENT=FACTOR",
    help="Scale one agent's volume, e.g. alice=0.6. Repeatable.",
)
@click.option(
    "--map",
    "mappings",
    multiple=True,
    metavar="KEY=SOUND",
    help="Override a sound, e.g. tool:shell=sawing or commit= (empty silences). Repeatable.",
)
@click.option("--only-agent", multiple=True, help="Only play events from these agents.")
@click.option("--min-gap", default=0.15, show_default=True, help="Min seconds between tool sounds.")
@click.option("--sounds-dir", type=click.Path(file_okay=False), help="gptme media dir.")
@click.option("--dry-run", is_flag=True, help="Print what would play instead of playing.")
@click.option("--list", "list_sounds", is_flag=True, help="Show the event-to-sound mapping.")
def sound(
    volume: float,
    agent_volume: tuple[str, ...],
    mappings: tuple[str, ...],
    only_agent: tuple[str, ...],
    min_gap: float,
    sounds_dir: str | None,
    dry_run: bool,
    list_sounds: bool,
) -> None:
    """Play gptme sound effects for events read as JSON lines from stdin."""
    mapping = _pairs(mappings, "--map")
    if list_sounds:
        for key, name in {**DEFAULT_SOUNDS, **mapping}.items():
            click.echo(f"{key:16} {name or '(silent)'}")
        return
    directory = Path(sounds_dir) if sounds_dir else find_sounds_dir()
    if directory is None and not dry_run:
        raise click.ClickException(
            "gptme's sounds were not found; install gptme or pass --sounds-dir"
        )
    try:
        factors = {k: float(v) for k, v in _pairs(agent_volume, "--agent-volume").items()}
    except ValueError as e:
        raise click.BadParameter(f"--agent-volume factor must be a number: {e}") from e
    player = SoundPlayer(
        directory or Path("."),
        mapping=mapping,
        volume=volume,
        agent_volume=factors,
        min_gap=min_gap,
        dry_run=dry_run,
    )
    try:
        for line in sys.stdin:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            event = AgentEvent.from_dict(data)
            if only_agent and event.agent not in only_agent:
                continue
            player.handle(event)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
