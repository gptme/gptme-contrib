#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Validate GPTME_HEARTBEAT JSONL event streams.

Reads heartbeat events as JSONL (one JSON object per line) from a file or
stdin and checks each event against the protocol envelope:

- required envelope fields are present and non-empty
- ``protocol`` is the literal ``gptme-heartbeat``
- ``type`` is a known lifecycle event
- ``status`` / ``invocation.finished`` state values are in the valid set
- ``cost`` events carry the required ``provider`` and ``model`` fields
- ``event_id`` is unique within an invocation

Stdlib only — no jsonschema dependency. The companion JSON Schema lives at
``schemas/gptme-heartbeat-event.schema.json``; this validator intentionally
mirrors it so a heartbeat sink can be checked without extra packages.

See ``docs/protocols/gptme-heartbeat.md`` for the full spec.

Usage:
    gptme-heartbeat-validate.py events.jsonl
    cat events.jsonl | gptme-heartbeat-validate.py -
    gptme-heartbeat-validate.py events.jsonl --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

PROTOCOL = "gptme-heartbeat"

REQUIRED_ENVELOPE_FIELDS = (
    "protocol",
    "version",
    "event_id",
    "invocation_id",
    "agent_id",
    "type",
    "occurred_at",
)

EVENT_TYPES = frozenset(
    {
        "invocation.started",
        "status",
        "cost",
        "cancellation.requested",
        "invocation.finished",
    }
)

STATUS_STATES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "waiting",
        "cancelling",
        "succeeded",
        "failed",
        "cancelled",
    }
)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    events_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def _validate_event(event: dict, line_no: int) -> list[str]:
    """Validate a single event envelope. Returns a list of error strings."""
    errors: list[str] = []

    def err(msg: str) -> None:
        errors.append(f"line {line_no}: {msg}")

    for fname in REQUIRED_ENVELOPE_FIELDS:
        if fname not in event:
            err(f"missing required field '{fname}'")
        elif isinstance(event[fname], str) and not event[fname]:
            err(f"field '{fname}' must not be empty")

    if event.get("protocol") not in (None, PROTOCOL):
        err(f"protocol must be '{PROTOCOL}', got {event['protocol']!r}")

    etype = event.get("type")
    if etype is not None and etype not in EVENT_TYPES:
        err(f"unknown event type {etype!r}")

    seq = event.get("sequence")
    if seq is not None and (not isinstance(seq, int) or seq < 1):
        err(f"sequence must be an integer >= 1, got {seq!r}")

    data = event.get("data")
    if data is not None and not isinstance(data, dict):
        err(f"data must be an object, got {type(data).__name__}")
        data = None

    if etype == "status" and isinstance(data, dict) and "state" in data:
        if data["state"] not in STATUS_STATES:
            err(f"status state {data['state']!r} not in {sorted(STATUS_STATES)}")

    if etype == "invocation.finished" and isinstance(data, dict) and "state" in data:
        if data["state"] not in TERMINAL_STATES:
            err(
                f"invocation.finished state {data['state']!r} "
                f"not in {sorted(TERMINAL_STATES)}"
            )

    if etype == "cost" and isinstance(data, dict):
        for required in ("provider", "model"):
            if not data.get(required):
                err(f"cost event missing required data field '{required}'")

    return errors


def validate_stream(lines) -> ValidationResult:
    result = ValidationResult()
    # Track event_id uniqueness per invocation.
    seen: dict[str, set[str]] = {}

    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            result.errors.append(f"line {line_no}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(event, dict):
            result.errors.append(f"line {line_no}: event must be a JSON object")
            continue

        result.events_checked += 1
        result.errors.extend(_validate_event(event, line_no))

        inv = event.get("invocation_id")
        eid = event.get("event_id")
        if isinstance(inv, str) and isinstance(eid, str) and inv and eid:
            ids = seen.setdefault(inv, set())
            if eid in ids:
                result.errors.append(
                    f"line {line_no}: duplicate event_id {eid!r} "
                    f"within invocation {inv!r}"
                )
            ids.add(eid)

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "path",
        help="JSONL file to validate, or '-' for stdin.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON summary instead of human text.",
    )
    args = parser.parse_args(argv)

    if args.path == "-":
        result = validate_stream(sys.stdin)
    else:
        with open(args.path, encoding="utf-8") as fh:
            result = validate_stream(fh)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "events_checked": result.events_checked,
                    "errors": result.errors,
                }
            )
        )
    else:
        if result.ok:
            print(f"OK: {result.events_checked} event(s) valid")
        else:
            for e in result.errors:
                print(f"ERROR: {e}", file=sys.stderr)
            print(
                f"FAILED: {len(result.errors)} error(s) "
                f"across {result.events_checked} event(s)",
                file=sys.stderr,
            )

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
