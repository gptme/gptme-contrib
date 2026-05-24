"""``aw-watcher-agent`` CLI: emit AI-assistant session activity to a local aw-server.

Subcommands:
    ensure-bucket   Create the session bucket if missing.
    emit-start      Record the start of an AI session (zero-duration placeholder).
    emit-end        Replace the placeholder with one event carrying duration + outcome.

Designed to be called from harness lifecycle hooks (e.g. Claude Code
SessionStart/Stop). Failures are non-fatal by default (``--strict`` to opt in)
so a watcher problem never breaks the agent session it observes.
"""

from __future__ import annotations

import argparse
import socket
import sys
from datetime import datetime

from .client import AWClient, AWClientError, Event, utc_now_iso
from . import core


def _hostname(explicit: str | None) -> str:
    return explicit or socket.gethostname()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _add_session_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--harness", help="gptme / claude-code / codex")
    p.add_argument("--model", help="resolved model id, e.g. claude-opus-4-7")
    p.add_argument("--category", help="session category if known")
    p.add_argument("--session-id", dest="session_id", help="harness session hash")
    p.add_argument("--trigger", help="autonomous / interactive / timer")
    p.add_argument("--workspace", help="workspace / agent name")
    p.add_argument("--hostname", help="override hostname (default: socket.gethostname())")
    p.add_argument("--server", default=core_default_server(), help="aw-server base URL")


def core_default_server() -> str:
    from .client import DEFAULT_SERVER

    return DEFAULT_SERVER


def cmd_ensure_bucket(args: argparse.Namespace) -> int:
    client = AWClient(args.server)
    host = _hostname(args.hostname)
    bid = core.bucket_id(host)
    created = client.ensure_bucket(bid, core.BUCKET_TYPE, core.CLIENT_NAME, host)
    print(f"bucket {bid}: {'created' if created else 'already exists'}")
    return 0


def cmd_emit_start(args: argparse.Namespace) -> int:
    client = AWClient(args.server)
    host = _hostname(args.hostname)
    bid = core.bucket_id(host)
    client.ensure_bucket(bid, core.BUCKET_TYPE, core.CLIENT_NAME, host)

    start = utc_now_iso()
    data = core.session_data(vars(args))
    event = Event(timestamp=start, duration=0.0, data=data)
    # Use heartbeat, not POST /events: aw-server's events endpoint returns null,
    # but heartbeat returns the created event (with id) so emit-end can delete
    # this placeholder and leave a single clean block.
    event_id = client.heartbeat(bid, event, pulsetime=0.0)

    sid = args.session_id or start
    core.write_state(sid, {"bucket_id": bid, "event_id": event_id, "start": start, "data": data})
    print(f"session start emitted: {bid} event_id={event_id} session_id={sid}")
    return 0


def cmd_emit_end(args: argparse.Namespace) -> int:
    client = AWClient(args.server)
    host = _hostname(args.hostname)
    bid = core.bucket_id(host)
    client.ensure_bucket(bid, core.BUCKET_TYPE, core.CLIENT_NAME, host)

    sid = args.session_id or ""
    state = core.read_state(sid) if sid else None
    end = utc_now_iso()

    if state and state.get("start"):
        start = state["start"]
        duration = (_parse_iso(end) - _parse_iso(start)).total_seconds()
        # Drop the zero-duration placeholder so we end with one clean block.
        if state.get("event_id") is not None:
            if not client.delete_event(state.get("bucket_id", bid), state["event_id"]):
                print(
                    f"warning: failed to delete placeholder event {state['event_id']} "
                    f"from {state.get('bucket_id', bid)} — timeline may contain duplicate block",
                    file=sys.stderr,
                )
    else:
        # No recorded start (crash / hook gap): fall back to provided duration.
        start = end
        duration = float(args.duration or 0.0)

    # Merge saved start metadata with any fields supplied at emit-end time.
    # CLI args at emit-end take precedence (allows corrections), outcome appended.
    saved_data: dict[str, str] = (state.get("data") or {}) if state else {}
    cli_overrides = core.session_data(vars(args))
    merged = {**saved_data, **cli_overrides}
    if args.outcome:
        merged["outcome"] = str(args.outcome)
    data = merged
    event = Event(timestamp=start, duration=max(duration, 0.0), data=data)
    # pulsetime=0 forces a fresh event (the outcome field differs from any prior
    # block anyway); heartbeat returns the id for logging where POST does not.
    event_id = client.heartbeat(bid, event, pulsetime=0.0)
    if sid:
        core.clear_state(sid)
    print(
        f"session end emitted: {bid} event_id={event_id} "
        f"duration={duration:.1f}s outcome={args.outcome}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aw-watcher-agent", description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit non-zero on errors")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ensure = sub.add_parser("ensure-bucket", help="create the session bucket")
    p_ensure.add_argument("--hostname")
    p_ensure.add_argument("--server", default=core_default_server())
    p_ensure.set_defaults(func=cmd_ensure_bucket)

    p_start = sub.add_parser("emit-start", help="record session start")
    _add_session_args(p_start)
    p_start.set_defaults(func=cmd_emit_start)

    p_end = sub.add_parser("emit-end", help="record session end with outcome")
    _add_session_args(p_end)
    p_end.add_argument("--outcome", help="productive / blocked / noop")
    p_end.add_argument("--duration", type=float, help="fallback duration (s) if no start state")
    p_end.set_defaults(func=cmd_emit_end)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AWClientError, OSError) as exc:
        msg = f"aw-watcher-agent: {exc}"
        if args.strict:
            print(msg, file=sys.stderr)
            return 1
        # Non-fatal by default: observing a session must never break it.
        print(f"{msg} (ignored; use --strict to fail)", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
