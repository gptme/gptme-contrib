"""CLI for generic inter-agent coordination.

Provides work-claiming, messaging, and status commands for any gptme agent.

Usage:
    gptme-coordination work-submit <task_id> [--metadata TEXT]
    gptme-coordination work-claim <agent_id> <task_id> [--ttl MINUTES]
    gptme-coordination work-complete <agent_id> <task_id> [--result TEXT]
    gptme-coordination work-abandon <agent_id> <task_id> [--reason TEXT]
    gptme-coordination work-list [--agent AGENT_ID] [--available | --claimed]
    gptme-coordination inbox <agent_id>
    gptme-coordination send <agent_id> <body> [--to RECIPIENT] [--channel CHANNEL]
    gptme-coordination announce <agent_id>
    gptme-coordination status
"""

from __future__ import annotations

import argparse
import sys

from gptme_coordination.db import (
    DEFAULT_DB_PATH,
    CoordinationDB,
    resolve_coordination_db_path,
)
from gptme_coordination.events import EventQueue
from gptme_coordination.messages import MessageBus
from gptme_coordination.work import WorkClaim, WorkClaimManager


def get_db_path() -> str:
    """Resolve the coordination DB path."""
    return str(resolve_coordination_db_path())


def cmd_work_submit(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        work = WorkClaimManager(db)
        claim = work.submit(args.task_id, metadata=args.metadata)
        print(f"submitted {claim.task_id} (status: {claim.status})")
        return 0


def cmd_work_claim(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        work = WorkClaimManager(db)
        claim = work.claim(
            args.agent_id,
            args.task_id,
            ttl_minutes=args.ttl,
        )
        if claim:
            print(f"claimed {claim.task_id} (expires {claim.expires_at})")
            return 0
        existing = work.get(args.task_id)
        holder = existing.claimer if existing else "unknown"
        print(f"DENIED — {args.task_id} held by {holder}", file=sys.stderr)
        return 1


def cmd_work_complete(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        work = WorkClaimManager(db)
        ok = work.complete(args.agent_id, args.task_id, result=args.result)
        if ok:
            print(f"completed {args.task_id}")
            return 0
        print(
            f"FAILED — no active claim on {args.task_id} by {args.agent_id}",
            file=sys.stderr,
        )
        return 1


def cmd_work_abandon(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        work = WorkClaimManager(db)
        ok = work.abandon(args.agent_id, args.task_id, reason=args.reason)
        if ok:
            print(f"abandoned {args.task_id}")
            return 0
        print(
            f"FAILED — no active claim on {args.task_id} by {args.agent_id}",
            file=sys.stderr,
        )
        return 1


def cmd_work_list(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        work = WorkClaimManager(db)
        if args.available:
            claims = work.list_available()
        elif args.claimed:
            claims = work.list_claimed(agent_id=args.agent)
        else:
            claims = work.list_all()

        if not claims:
            print("(no work items)")
            return 0

        for claim in claims:
            parts = [f"{claim.task_id} [{claim.status}]"]
            if claim.claimer:
                parts.append(f"by {claim.claimer}")
            if claim.expires_at:
                parts.append(f"expires {claim.expires_at}")
            print("  " + " ".join(parts))
        return 0


def _format_claim(claim: WorkClaim) -> str:
    parts = [f"{claim.task_id} [{claim.status}]"]
    if claim.claimer:
        parts.append(f"by {claim.claimer}")
    if claim.expires_at:
        parts.append(f"expires {claim.expires_at}")
    return " ".join(parts)


def cmd_inbox(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        bus = MessageBus(db)
        messages = bus.inbox(args.agent_id)
        if not messages:
            print(f"(no messages for {args.agent_id})")
            return 0
        for msg in messages:
            src = msg.sender
            dest = f"→{msg.recipient}" if msg.recipient else "(broadcast)"
            print(f"[{msg.created_at}] {src} {dest} [{msg.channel}]: {msg.body}")
        return 0


def cmd_send(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        bus = MessageBus(db)
        msg = bus.send(
            args.agent_id,
            args.body,
            recipient=args.to,
            channel=args.channel,
        )
        dest = f"→{msg.recipient}" if msg.recipient else "(broadcast)"
        print(f"sent message {msg.id} {dest} [{msg.channel}]")
        return 0


def cmd_announce(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        bus = MessageBus(db)
        msg = bus.send(
            args.agent_id,
            f"{args.agent_id} is active",
            channel="announce",
        )
        print(f"announced presence (message {msg.id})")
        return 0


def cmd_queue_list(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        queue = EventQueue(db)
        events = queue.list_events(state=args.state, repo=args.repo, limit=args.limit)
        if not events:
            print("(no events)")
            return 0
        for ev in events:
            age = ""
            if ev.state == "pending":
                age = f" eff={ev.effective_priority}"
            title_part = f" — {ev.title}" if ev.title else ""
            print(
                f"[{ev.id}] {ev.state:<12} {ev.trigger_type:<22} {ev.thread_key}{title_part}{age}"
            )
        return 0


def cmd_queue_stats(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        queue = EventQueue(db)
        stats = queue.stats()
        print(f"pending:     {stats.pending}")
        print(f"claimed:     {stats.claimed}")
        print(f"completed:   {stats.completed}")
        print(f"dead_letter: {stats.dead_letter}")
        if stats.oldest_pending_seconds is not None:
            minutes = int(stats.oldest_pending_seconds / 60)
            print(f"oldest_pending: {minutes}m")
        return 0


def cmd_queue_ingest(args: argparse.Namespace) -> int:
    import json as _json

    db_path = args.db or get_db_path()
    payload = _json.loads(args.payload) if args.payload else None
    with CoordinationDB(db_path) as db:
        queue = EventQueue(db)
        event = queue.ingest(
            trigger_type=args.trigger_type,
            source=args.source,
            thread_key=args.thread_key,
            external_id=args.external_id,
            repo=args.repo,
            number=args.number,
            title=args.title,
            url=args.url,
            payload=payload,
            priority=args.priority,
            dedup_window_minutes=args.dedup_window,
        )
        if event is None:
            print(f"skipped (duplicate within dedup window): {args.thread_key}")
            return 0
        print(
            f"ingested event {event.id} ({event.trigger_type}) thread={event.thread_key}"
        )
        return 0


def cmd_queue_retry(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        queue = EventQueue(db)
        ok = queue.retry(args.event_id)
        if ok:
            print(f"retrying event {args.event_id}")
            return 0
        print(
            f"FAILED — event {args.event_id} not in dead_letter state",
            file=sys.stderr,
        )
        return 1


def cmd_queue_discard(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        queue = EventQueue(db)
        ok = queue.discard(args.event_id)
        if ok:
            print(f"discarded event {args.event_id}")
            return 0
        print(
            f"FAILED — event {args.event_id} not found or already completed",
            file=sys.stderr,
        )
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    db_path = args.db or get_db_path()
    with CoordinationDB(db_path) as db:
        work = WorkClaimManager(db)
        bus = MessageBus(db)
        claimed = work.list_claimed()
        recent = bus.history(channel="announce", limit=10)
        print(f"DB: {db_path}")
        print(f"Active claims: {len(claimed)}")
        for c in claimed:
            print(f"  {_format_claim(c)}")
        print(f"Recent announcements: {len(recent)}")
        for m in recent:
            print(f"  [{m.created_at}] {m.body}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gptme-coordination",
        description="Inter-agent coordination: work claims, messaging",
    )
    parser.add_argument("--db", help=f"DB path (default: {DEFAULT_DB_PATH})")
    sub = parser.add_subparsers(dest="command")

    # work-submit
    p = sub.add_parser("work-submit", help="Submit a task as available")
    p.add_argument("task_id")
    p.add_argument("--metadata", help="Optional metadata")

    # work-claim
    p = sub.add_parser("work-claim", help="Claim a task")
    p.add_argument("agent_id")
    p.add_argument("task_id")
    p.add_argument("--ttl", type=int, default=60, help="TTL in minutes (default: 60)")

    # work-complete
    p = sub.add_parser("work-complete", help="Mark task complete")
    p.add_argument("agent_id")
    p.add_argument("task_id")
    p.add_argument("--result", help="Optional result text")

    # work-abandon
    p = sub.add_parser("work-abandon", help="Abandon a task claim")
    p.add_argument("agent_id")
    p.add_argument("task_id")
    p.add_argument("--reason", help="Optional reason")

    # work-list
    p = sub.add_parser("work-list", help="List work items")
    p.add_argument("--agent", help="Filter by agent ID")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--available", action="store_true")
    g.add_argument("--claimed", action="store_true")

    # inbox
    p = sub.add_parser("inbox", help="Read messages")
    p.add_argument("agent_id")

    # send
    p = sub.add_parser("send", help="Send a message")
    p.add_argument("agent_id", help="Sender ID")
    p.add_argument("body")
    p.add_argument("--to", dest="to", help="Recipient agent ID (omit = broadcast)")
    p.add_argument("--channel", default="general")

    # announce
    p = sub.add_parser("announce", help="Announce presence")
    p.add_argument("agent_id")

    # status
    sub.add_parser("status", help="Show coordination DB status")

    # queue list
    p = sub.add_parser("queue-list", help="List events in the queue")
    p.add_argument(
        "--state",
        help="Filter by state (pending, claimed, completed, dead_letter)",
    )
    p.add_argument("--repo", help="Filter by repo (owner/repo)")
    p.add_argument(
        "--limit", type=int, default=50, help="Max events to show (default: 50)"
    )

    # queue stats
    sub.add_parser("queue-stats", help="Show event queue statistics")

    # queue ingest
    p = sub.add_parser("queue-ingest", help="Ingest an event into the queue")
    p.add_argument(
        "trigger_type", help="Event trigger type (e.g. pr_update, ci_failure_pr)"
    )
    p.add_argument("source", help="Event source (e.g. github, systemd)")
    p.add_argument("thread_key", help="Dedup key (e.g. gptme/gptme:2949)")
    p.add_argument("--external-id", help="External ID (e.g. GitHub notification ID)")
    p.add_argument("--repo", help="Repository (owner/repo)")
    p.add_argument("--number", type=int, help="Issue/PR number")
    p.add_argument("--title", help="Event title")
    p.add_argument("--url", help="Event URL")
    p.add_argument("--payload", help="JSON payload string")
    p.add_argument(
        "--priority", type=int, help="Override priority (default: from trigger type)"
    )
    p.add_argument(
        "--dedup-window",
        type=int,
        default=30,
        help="Dedup window in minutes (default: 30)",
    )

    # queue retry
    p = sub.add_parser("queue-retry", help="Retry a dead-lettered event")
    p.add_argument("event_id", type=int, help="Event ID to retry")

    # queue discard
    p = sub.add_parser("queue-discard", help="Discard an event")
    p.add_argument("event_id", type=int, help="Event ID to discard")

    return parser


COMMANDS = {
    "work-submit": cmd_work_submit,
    "work-claim": cmd_work_claim,
    "work-complete": cmd_work_complete,
    "work-abandon": cmd_work_abandon,
    "work-list": cmd_work_list,
    "inbox": cmd_inbox,
    "send": cmd_send,
    "announce": cmd_announce,
    "status": cmd_status,
    "queue-list": cmd_queue_list,
    "queue-stats": cmd_queue_stats,
    "queue-ingest": cmd_queue_ingest,
    "queue-retry": cmd_queue_retry,
    "queue-discard": cmd_queue_discard,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1
    handler = COMMANDS.get(args.command)
    if handler is None:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
