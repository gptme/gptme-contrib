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
from .gitwatch import GitWatcher
from .sound import DEFAULT_SOUNDS, SoundPlayer, describe, find_sounds_dir


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
@click.option(
    "--repo",
    "repos",
    multiple=True,
    metavar="DIR",
    help="Watch this repo's commits/pushes from the start (others are added as agents touch them).",
)
@click.option(
    "--no-git",
    is_flag=True,
    help="Don't watch git reflogs; report commits/pushes from shell output only.",
)
@click.option("--from-start", is_flag=True, help="Replay recently active files first.")
@click.option("--once", is_flag=True, help="Do one pass and exit (with --from-start: replay).")
@click.option("--active-window", default=600.0, show_default=True, help="Seconds of idleness.")
@click.option("--interval", default=0.5, show_default=True, help="Poll interval in seconds.")
def tail(
    agent: str,
    harnesses: tuple[str, ...],
    roots: tuple[str, ...],
    repos: tuple[str, ...],
    no_git: bool,
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

    git = None if no_git else GitWatcher(agent)
    for repo in repos:
        if git is not None and git.watch(os.path.abspath(os.path.expanduser(repo))) is None:
            raise click.BadParameter(f"not inside a git work tree: {repo}")
    follower = Follower(sources, agent, active_window=active_window, from_start=from_start, git=git)
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
@click.option("--dry-run", is_flag=True, help="Don't play anything; just print the lines.")
@click.option("--quiet", "-q", is_flag=True, help="Don't print a line per event.")
@click.option("--list", "list_sounds", is_flag=True, help="Show the event-to-sound mapping.")
def sound(
    volume: float,
    agent_volume: tuple[str, ...],
    mappings: tuple[str, ...],
    only_agent: tuple[str, ...],
    min_gap: float,
    sounds_dir: str | None,
    dry_run: bool,
    quiet: bool,
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
            sound_name = player.handle(event)
            if not quiet:
                click.echo(describe(event, sound_name, player.last_status))
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass


@main.command()
@click.option(
    "--agent",
    "agents",
    multiple=True,
    required=True,
    metavar="NAME[=FEED_ID]",
    help="Publish this stream agent (as FEED_ID, default NAME). Repeatable; others are ignored.",
)
@click.option("--supabase-url", envvar="GPTME_FEED_SUPABASE_URL", help="Supabase project URL.")
@click.option("--anon-key", envvar="GPTME_FEED_ANON_KEY", help="Supabase anon (public) key.")
@click.option(
    "--token-cmd",
    envvar="GPTME_FEED_TOKEN_CMD",
    help="Command that prints an access token for the account that owns the agents.",
)
@click.option("--min-interval", default=20.0, show_default=True, help="Seconds between writes.")
@click.option("--heartbeat", default=240.0, show_default=True, help="Refresh while working (s).")
@click.option("--idle-after", default=300.0, show_default=True, help="Quiet seconds before idle.")
@click.option("--dry-run", is_flag=True, help="Print rows as JSON instead of writing them.")
def publish(
    agents: tuple[str, ...],
    supabase_url: str | None,
    anon_key: str | None,
    token_cmd: str | None,
    min_interval: float,
    heartbeat: float,
    idle_after: float,
    dry_run: bool,
) -> None:
    """Keep each agent's public status row up to date from events on stdin.

    Only public-safe facts are written: working/idle, the activity class, the
    latest milestone in a public GitHub repo, and a count of other commits.
    """
    import queue
    import threading

    from .publish import Publisher, SupabaseWriter

    mapping = {}
    for spec in agents:
        name, _, feed_id = spec.partition("=")
        mapping[name] = feed_id or name

    if dry_run:

        def write(row: dict) -> bool:
            click.echo(json.dumps(row))
            sys.stdout.flush()
            return True

    else:
        if not (supabase_url and anon_key and token_cmd):
            raise click.UsageError("--supabase-url, --anon-key and --token-cmd are required")
        write = SupabaseWriter(supabase_url, anon_key, token_cmd)

    pub = Publisher(
        mapping, write, min_interval=min_interval, heartbeat=heartbeat, idle_after=idle_after
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        for line in sys.stdin:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    try:
        while True:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                line = ""
            if line is None:
                pub.tick(flush=True)
                return
            if line.strip():
                try:
                    data = json.loads(line)
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    pub.handle(AgentEvent.from_dict(data))
            pub.tick()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
