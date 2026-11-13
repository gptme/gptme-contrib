"""Uptime Kuma push helper — one correct implementation for every caller.

Part of gptme/gptme-contrib#2171: each agent had built its own push sender, and
the copies failed differently. Gordon's `_kuma.py` records the incident that
made a shared helper worth extracting: a caller built the push URL by string
concatenation with an unencoded `msg` ("off-hours probe ok"). curl rejects a URL
containing a space (exit 3, "malformed"), and `check=False` hid the failure, so
the log said "pushing Kuma heartbeat" while nothing ever reached Kuma (monitor
62 DOWN for weeks). The same class of bug is a *silent* one: the monitor looks
configured and nothing pages.

Contract enforced here:

* `status` and `msg` are URL-encoded, always.
* A failed push is **logged** (stderr): non-zero curl rc, or a body that is not
  `{"ok": true,...}`. Silence is the failure this module exists to prevent.
* The return value is `True` only when Kuma acknowledged the push.
* The push URL is secret-lite and never appears in output, logs or exceptions.
* An *acknowledged* push is stamped in a durable ledger, so a later reader can
  tell "the pusher stopped running / the push has been failing" from "the
  monitor is legitimately quiet". No push is the signal; a stale ack is how we
  notice our side of it.

Usage (Python)::

    from kuma_push import push
    if not push("KUMA_PUSH_URL_INFERENCE", "up", "healthy"):
        ...  # already logged; keep the run going, the monitor pages on silence

Usage (shell): ``python3 scripts/kuma_push.py record-ack KEY`` after a shell
caller got an ``"ok":true`` body of its own.

URL resolution order for ``key``: the process environment, then ``KEY=value``
lines in the env file (default ``~/.config/uptime-kuma/push.env``). Callers
whose URL is already in another variable (e.g. ``BOB_AUTH_PAGER_KUMA_PUSH_URL``)
pass it to :func:`push_url` directly instead.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# Secret-lite push URLs. Mode 600, outside git, never read from a module constant
# that gets printed.
PUSH_ENV = Path.home() / ".config" / "uptime-kuma" / "push.env"
# Durable ack ledger: last acknowledged push per key. Background state, never
# auto-committed.
ACK_FILE = Path.cwd() / "state" / "kuma-last-ack.json"
# Hard cap so a hung endpoint cannot stall an alert path. The caller must never
# fail because of a heartbeat.
PUSH_TIMEOUT_SECONDS = 10
# How many multiples of the expected interval may pass before an ack is stale.
STALE_FACTOR = 3.0


def load_push_url(key: str, env_file: Path = PUSH_ENV) -> str | None:
    """Return the push URL for ``key``; env first, then the env file.

    ``None`` when unconfigured — that is a deploy-time state, not an error. The
    URL is never logged by this module.
    """
    value = os.environ.get(key, "").strip()
    if value:
        return value
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, raw = line.partition("=")
            if name.strip() != key:
                continue
            url = raw.strip().strip('"').strip("'")
            return url or None
    except OSError:
        return None
    return None


def build_push_url(push_url: str, status: str, msg: str) -> str:
    """Build the push URL with encoding applied.

    ``quote_via=quote`` keeps the encoding close to what the Kuma docs show
    (spaces become ``%20``) while still escaping every reserved character.
    """
    parts = urllib.parse.urlsplit(push_url)
    # Kuma's generated URL already includes status/msg. Duplicate parameters
    # become arrays on the server, so replace them rather than appending.
    params = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"status", "msg"}
    ]
    params.extend([("status", status), ("msg", msg)])
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit(parts._replace(query=query))


def response_ok(body: str) -> bool:
    """True only for Kuma's ``{"ok": true, ...}`` acknowledgement body."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def load_acks(path: Path | None = None) -> dict[str, str]:
    path = path or ACK_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record_ack(key: str, path: Path | None = None, now: datetime | None = None) -> bool:
    """Stamp ``key`` as acknowledged now. Atomic, never raises on I/O failure.

    Never raising is deliberate: a ledger write failure must not turn a
    successful heartbeat into a caller-visible failure. It is logged so the
    ledger being unwritable is itself observable.
    """
    return _update_ledger(key, path, now)


def start_tracking(path: Path | None = None, now: datetime | None = None) -> bool:
    """Start the bootstrap clock without claiming a successful heartbeat.

    Idempotent: failed pushes and repeated checker runs never reset the clock.
    Shell senders should call this from their ledger checker before load_acks().
    """
    return _update_ledger(None, path, now)


def _update_ledger(key: str | None, path: Path | None, now: datetime | None) -> bool:
    path = path or ACK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Lock a stable sidecar, not the ledger inode replaced below. Independent
        # timers must serialize the entire read/merge/replace to retain every ack.
        with path.with_suffix(path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            acks = load_acks(path)
            if key is None and _parse_ts(acks.get("_since")) is not None:
                return True
            stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
            # Bootstrap guard: a key that has never been acked is not "dark" until
            # tracking has been live for one full tolerance window.
            acks.setdefault("_since", stamp)
            if key is not None:
                acks[key] = stamp
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".kuma-ack-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(acks, fh, indent=1, sort_keys=True)
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
    except OSError as exc:
        print(f"WARN: could not record Kuma ack for {key}: {exc}", file=sys.stderr)
        return False
    return True


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stale_acks(
    acks: dict[str, str],
    keys: list[str],
    now: datetime,
    intervals: dict[str, float],
    factor: float = STALE_FACTOR,
) -> list[str]:
    """Human-readable problems for configured ``keys`` whose ack is stale/missing.

    ``intervals`` maps key -> expected push cadence in hours; keys without an
    expectation are skipped (the helper cannot invent a cadence for a monitor it
    does not own). A missing ack is only reported once ``_since`` is older than
    the tolerance window, so enabling tracking does not page on the first run.
    """
    problems: list[str] = []
    since = _parse_ts(acks.get("_since"))
    for key in keys:
        expected_h = intervals.get(key)
        if expected_h is None:
            continue
        limit_h = expected_h * factor
        raw = acks.get(key)
        if not raw:
            if since is None:
                problems.append(
                    f"Kuma push {key}: tracking not initialized; call start_tracking()"
                )
                continue
            if (now - since).total_seconds() / 3600 <= limit_h:
                continue
            problems.append(
                f"Kuma push {key}: no acknowledged push on record (expected every "
                f"{expected_h:g}h) — monitor may be dark"
            )
            continue
        ts = _parse_ts(raw)
        if ts is None:
            problems.append(f"Kuma push {key}: unparseable last-ack {raw!r}")
            continue
        age_h = (now - ts).total_seconds() / 3600
        if age_h > limit_h:
            problems.append(
                f"Kuma push {key}: last acknowledged {age_h:.1f}h ago "
                f"(limit {limit_h:.2g}h) — push failing or pusher not running; "
                "Kuma is likely paging"
            )
    return problems


def configured_keys(env_file: Path = PUSH_ENV) -> list[str]:
    """Keys present in the env file (callers add their own environment keys)."""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    keys = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())
    return keys


def push_url(
    push_url_value: str,
    status: str = "up",
    msg: str = "OK",
    *,
    key: str | None = None,
    ack_path: Path | None = None,
    label: str | None = None,
    timeout: int = PUSH_TIMEOUT_SECONDS,
) -> bool:
    """Push ``status``/``msg`` to an already-resolved push URL.

    Returns True only on an acknowledged push. Failures are logged and never
    raised — a dead heartbeat endpoint must not take down the run that carries a
    real alert. ``key`` is only used for the ack ledger and the log label; the
    URL itself is never printed.
    """
    name = label or key or "kuma"
    if not push_url_value:
        print(f"WARN: Kuma push {name}: no push URL configured", file=sys.stderr)
        return False
    if key:
        start_tracking(path=ack_path)
    full_url = build_push_url(push_url_value, status, msg)
    try:
        result = subprocess.run(
            ["curl", "-sS", "-m", str(timeout), full_url],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # TimeoutExpired includes argv, including the secret capability URL.
        print(f"WARN: Kuma push {name} failed: {type(exc).__name__}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"WARN: Kuma push {name} curl rc={result.returncode}: "
            f"{result.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return False
    if not response_ok(result.stdout):
        print(
            f"WARN: Kuma push {name} not acknowledged: {result.stdout.strip()[:200]}",
            file=sys.stderr,
        )
        return False
    if key:
        record_ack(key, path=ack_path)
    return True


def push(
    key: str,
    status: str = "up",
    msg: str = "OK",
    *,
    env_file: Path = PUSH_ENV,
    ack_path: Path | None = None,
    label: str | None = None,
    timeout: int = PUSH_TIMEOUT_SECONDS,
) -> bool:
    """Resolve ``key`` to a push URL and push. See :func:`push_url`."""
    url = load_push_url(key, env_file)
    if not url:
        # Unset is a deploy-time state: no warning spam on every timer tick.
        return False
    return push_url(
        url, status, msg, key=key, ack_path=ack_path, label=label, timeout=timeout
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser(
        "record-ack",
        help="stamp KEY as acknowledged (shell callers that push themselves)",
    )
    record.add_argument("key")
    record.add_argument("--ack-file", default=None)

    send = sub.add_parser("push", help="push a heartbeat for KEY")
    send.add_argument("key")
    send.add_argument("--status", default="up", choices=["up", "down", "unknown"])
    send.add_argument("--msg", default="OK")
    send.add_argument("--env-file", default=str(PUSH_ENV))
    send.add_argument("--ack-file", default=None)

    args = parser.parse_args(argv)
    if args.command == "record-ack":
        ok = record_ack(args.key, path=Path(args.ack_file) if args.ack_file else None)
        return 0 if ok else 1
    ok = push(
        args.key,
        args.status,
        args.msg,
        env_file=Path(args.env_file),
        ack_path=Path(args.ack_file) if args.ack_file else None,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
