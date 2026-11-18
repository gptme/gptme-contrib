"""Hang watchdog for headless ``claude -p --output-format stream-json`` sessions.

Headless Claude Code sessions sometimes hang: the process stays alive but stops
emitting stream-json events, or never exits after its final ``result`` event.
An outer ``timeout`` only catches this at the very end of the session budget.
This watchdog wraps the session command, relays its stdout unchanged, parses
the stream-json events and stops the session early when it goes silent.

Trip conditions (each is checked against the time since the last stdout line):

* **idle**: no event for ``idle_free`` seconds while no tool call is pending.
* **tool_pending**: no event for ``idle_tooled`` seconds while at least one
  tool call is pending. Tool calls are tracked by id: an ``assistant`` event's
  ``tool_use`` block adds its id, a ``user`` event's ``tool_result`` block with
  a known ``tool_use_id`` removes it. Results with unknown ids (for example a
  subagent's own inner tool calls) are ignored, so they cannot clear the
  parent's pending call and shrink the allowance.
* **post_result**: the ``result`` event was seen but the process did not exit
  within ``grace`` seconds.
* **eof_no_exit**: stdout closed but the process did not exit within
  ``grace`` seconds. After a ``result`` event the earlier deadline (result
  plus ``grace``) still applies; closing stdout does not restart it.

On a trip the session's whole process group gets SIGTERM, then SIGKILL after
``kill_after`` seconds if any member of the group is still alive (even when
the session leader itself already exited). The session is started in its own
process group so tool subprocesses are stopped with it.

Output is relayed by a separate writer thread, so a downstream reader that
stops reading cannot stall supervision. After the session ends, undelivered
output is drained for at most ``grace`` seconds.

Exit code contract:

* the session's own exit code when it ends by itself (a signal death ``-N``
  maps to ``128 + N``);
* ``0`` after a **post_result** trip when the result was not an error (the
  work finished, only the exit hung), ``1`` when the result was an error;
* :data:`EXIT_HANG` (75) after an **idle**, **tool_pending** or
  **eof_no_exit** trip without a result;
* ``128 + signum`` when the watchdog itself is stopped by SIGTERM, SIGINT or
  SIGHUP (it stops the session first);
* ``2`` for a usage error.

On exit the watchdog writes exactly one verdict line: a JSON object with
``outcome`` (``ok``, ``error``, ``hang`` or ``interrupted``), ``trip``,
``reason`` and ``exit_code``, to ``--verdict-file`` when given (appended) and
to stderr prefixed with ``[claude-watchdog] verdict``.

The module only uses the standard library, so it can also run as a plain
script: ``python3 claude_watchdog.py -- claude -p ...``.
"""

# ruff: noqa: E402
from __future__ import annotations

import os
import sys

if __name__ == "__main__":  # pragma: no cover - exercised by the CLI tests
    # Run as a file, this package directory is sys.path[0], and the sibling
    # select.py would shadow the stdlib module that subprocess imports.
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]

import argparse
import json
import queue
import select
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import IO, Any

IDLE_FREE_DEFAULT = 120.0
"""Seconds without an event, with no tool call pending, before tripping."""
IDLE_TOOLED_DEFAULT = 1800.0
"""Seconds without an event, while a tool call is pending, before tripping."""
GRACE_DEFAULT = 30.0
"""Seconds the process may linger after its ``result`` event (or EOF)."""
KILL_AFTER_DEFAULT = 5.0
"""Seconds between SIGTERM and SIGKILL to the session's process group."""
POLL_DEFAULT = 1.0
"""Seconds between idle checks."""

EXIT_HANG = 75
"""Exit code after a hang trip (``EX_TEMPFAIL``: the session may be retried)."""
EXIT_USAGE = 2

ENV_PREFIX = "AGENT_CLAUDE_WATCHDOG_"
LOG_PREFIX = "[claude-watchdog]"


@dataclass
class StreamState:
    """What the watchdog knows about the session from its stream-json output."""

    pending_tools: dict[str, tuple[str, float]] = field(default_factory=dict)
    """Pending tool call id -> (tool name, monotonic time the call was seen)."""
    events: int = 0
    result_seen: bool = False
    result_is_error: bool = False

    def observe(self, line: bytes | str, now: float) -> None:
        """Update the state from one stdout line. Non-JSON lines are ignored."""
        if self.result_seen:
            return
        if isinstance(line, bytes):
            line = line.decode(errors="replace")
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(ev, dict):
            return
        self.events += 1
        ev_type = ev.get("type")
        if ev_type == "assistant":
            for block in _content_blocks(ev):
                if block.get("type") == "tool_use":
                    uid = block.get("id")
                    if isinstance(uid, str) and uid:
                        name = block.get("name")
                        self.pending_tools[uid] = (
                            name if isinstance(name, str) else "?",
                            now,
                        )
        elif ev_type == "user":
            for block in _content_blocks(ev):
                if block.get("type") == "tool_result":
                    uid = block.get("tool_use_id")
                    if isinstance(uid, str):
                        self.pending_tools.pop(uid, None)
        elif ev_type == "result":
            self.result_seen = True
            self.result_is_error = bool(ev.get("is_error")) or (
                ev.get("subtype") not in (None, "success")
            )

    def pending_summary(self, now: float) -> list[dict[str, Any]]:
        """Pending tool calls, oldest first, for the verdict."""
        items = sorted(self.pending_tools.items(), key=lambda kv: kv[1][1])
        return [
            {"id": uid, "name": name, "age_seconds": round(now - started, 1)}
            for uid, (name, started) in items
        ]


def _content_blocks(ev: dict[str, Any]) -> list[dict[str, Any]]:
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


@dataclass(frozen=True)
class WatchdogConfig:
    idle_free: float = IDLE_FREE_DEFAULT
    idle_tooled: float = IDLE_TOOLED_DEFAULT
    grace: float = GRACE_DEFAULT
    kill_after: float = KILL_AFTER_DEFAULT
    poll: float = POLL_DEFAULT


def _relay(raw: bytes, out_fd: int | None) -> None:
    """Write a line to the output fd, tolerating a slow or closed reader.

    A blocked downstream pipe (EAGAIN) waits until writable instead of raising;
    a closed downstream stops relaying but keeps the session supervised.
    """
    if out_fd is None:
        return
    mv = memoryview(raw)
    while mv:
        try:
            n = os.write(out_fd, mv)
            mv = mv[n:]
        except BlockingIOError:
            select.select([], [out_fd], [], 1.0)
        except OSError:
            return


class _Writer:
    """Relays output lines from a thread of its own.

    A downstream reader that stops reading blocks only this thread, never the
    supervision loop.
    """

    def __init__(self, out_fd: int | None) -> None:
        self._fd = out_fd
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._abandoned = threading.Event()
        self._thread: threading.Thread | None = None
        if out_fd is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            raw = self._lines.get()
            # Once abandoned, never write again: the fd may be closed and reused.
            if raw is None or self._abandoned.is_set():
                return
            _relay(raw, self._fd)

    def put(self, raw: bytes) -> None:
        if self._thread is not None:
            self._lines.put(raw)

    def close(self, timeout: float) -> bool:
        """Finish relaying; give up after ``timeout`` seconds. True if drained."""
        if self._thread is None:
            return True
        self._lines.put(None)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self._abandoned.set()
            return False
        return True


def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(proc: subprocess.Popen[bytes]) -> bool:
    """True while any process is left in the session's process group."""
    proc.poll()  # reap the leader, so a zombie leader does not count
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - a member we cannot signal
        return True
    return True


def terminate_group(proc: subprocess.Popen[bytes], kill_after: float) -> bool:
    """SIGTERM the process group, then SIGKILL it if any member is still alive.

    The leader exiting is not enough: a tool process that ignores SIGTERM
    keeps the group alive and gets SIGKILL after ``kill_after`` seconds.
    Returns True when SIGKILL was needed.
    """
    _signal_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + kill_after
    while _group_alive(proc):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    else:
        return False
    _signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL cannot be ignored
        pass
    return True


def _log(msg: str, err: IO[str]) -> None:
    try:
        print(f"{LOG_PREFIX} {msg}", file=err, flush=True)
    except (OSError, ValueError):
        pass


def _returncode_to_exit(rc: int | None) -> int:
    if rc is None:
        return 1
    if rc < 0:
        return 128 + (-rc)
    return rc


class _Interrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def run_watchdog(
    cmd: Sequence[str],
    config: WatchdogConfig,
    *,
    verdict_file: str | None = None,
    out: IO[bytes] | None = None,
    err: IO[str] | None = None,
    install_signal_handlers: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Run ``cmd`` under the watchdog. Returns ``(exit_code, verdict)``.

    ``out`` receives the session's stdout (default: this process's stdout).
    """
    err = err if err is not None else sys.stderr
    if out is None:
        out = sys.stdout.buffer
    try:
        out_fd: int | None = out.fileno()
    except (OSError, ValueError, AttributeError):
        out_fd = None

    started = time.monotonic()
    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=None,
        bufsize=0,
        start_new_session=True,
    )
    assert proc.stdout is not None
    stdout = proc.stdout

    lines: queue.Queue[bytes | None] = queue.Queue()

    def reader() -> None:
        try:
            for raw in stdout:
                lines.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    previous_handlers: dict[int, Any] = {}
    if install_signal_handlers:

        def on_signal(signum: int, _frame: object) -> None:
            raise _Interrupted(signum)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                previous_handlers[sig] = signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass

    writer = _Writer(out_fd)
    state = StreamState()
    last_line = time.monotonic()
    result_at: float | None = None
    eof_at: float | None = None
    trip: str | None = None
    reason = ""
    interrupted_by: int | None = None
    escalated = False
    idle_seconds = 0.0
    pending_at_trip: list[dict[str, Any]] = []

    try:
        while True:
            now = time.monotonic()
            if proc.poll() is not None and (eof_at is not None or lines.empty()):
                # Exited. Lines still queued are relayed below; once the queue
                # is drained we stop even if a leftover grandchild keeps the
                # pipe open.
                break
            if eof_at is not None:
                # After a result the earlier result deadline still applies.
                since = result_at if result_at is not None else eof_at
                if now - since >= config.grace:
                    trip = "post_result" if state.result_seen else "eof_no_exit"
                    what = (
                        "'result' event seen"
                        if result_at is not None
                        else "stdout closed"
                    )
                    reason = f"{what} but process still running after {config.grace:g}s"
                    break
                time.sleep(min(config.poll, 0.1))
                continue
            if result_at is not None and now - result_at >= config.grace:
                trip = "post_result"
                reason = f"'result' event seen but process still running after {config.grace:g}s"
                break
            idle_seconds = now - last_line
            pending = bool(state.pending_tools)
            limit = config.idle_tooled if pending else config.idle_free
            if result_at is None and idle_seconds >= limit:
                pending_at_trip = state.pending_summary(now)
                if pending:
                    trip = "tool_pending"
                    names = ", ".join(
                        f"{p['name']} ({p['id']})" for p in pending_at_trip
                    )
                    reason = (
                        f"no stream event for {idle_seconds:.1f}s "
                        f"(limit {limit:g}s with a tool pending: {names})"
                    )
                else:
                    trip = "idle"
                    reason = (
                        f"no stream event for {idle_seconds:.1f}s "
                        f"(limit {limit:g}s with no tool pending)"
                    )
                break

            wait = config.poll
            if result_at is not None:
                wait = min(wait, max(0.0, config.grace - (now - result_at)))
            else:
                wait = min(wait, max(0.0, limit - idle_seconds))
            try:
                raw = lines.get(timeout=max(wait, 0.01))
            except queue.Empty:
                continue
            if raw is None:
                eof_at = time.monotonic()
                continue
            last_line = time.monotonic()
            writer.put(raw)
            was_result = state.result_seen
            state.observe(raw, last_line)
            if state.result_seen and not was_result:
                result_at = last_line

        if trip is not None:
            _log(f"{reason}; terminating session process group", err)
            escalated = terminate_group(proc, config.kill_after)
            if escalated:
                _log(f"SIGTERM ignored for {config.kill_after:g}s; sent SIGKILL", err)
    except _Interrupted as exc:
        interrupted_by = exc.signum
        trip = "signal"
        reason = f"watchdog received signal {exc.signum}"
        _log(f"{reason}; terminating session process group", err)
        escalated = terminate_group(proc, config.kill_after)
    finally:
        if proc.poll() is None:
            # Never leave the session behind, whatever happened above.
            escalated = terminate_group(proc, config.kill_after) or escalated
        for prev_sig, handler in previous_handlers.items():
            try:
                signal.signal(prev_sig, handler)
            except (ValueError, OSError):
                pass

    # Relay anything the session wrote before it exited.
    reader_thread.join(timeout=1.0)
    while True:
        try:
            raw = lines.get_nowait()
        except queue.Empty:
            break
        if raw is None:
            continue
        writer.put(raw)
        state.observe(raw, time.monotonic())
    if not writer.close(timeout=config.grace):
        _log(
            f"downstream reader stalled; gave up relaying output after {config.grace:g}s",
            err,
        )

    child_rc = proc.returncode
    if interrupted_by is not None:
        outcome = "interrupted"
        exit_code = 128 + interrupted_by
    elif trip == "post_result":
        outcome = "error" if state.result_is_error else "ok"
        exit_code = 1 if state.result_is_error else 0
    elif trip is not None:
        outcome = "hang"
        exit_code = EXIT_HANG
    else:
        exit_code = _returncode_to_exit(child_rc)
        outcome = "ok" if exit_code == 0 else "error"
        reason = f"session exited with code {exit_code}"

    verdict: dict[str, Any] = {
        "watchdog": "claude-p",
        "outcome": outcome,
        "trip": trip,
        "reason": reason,
        "exit_code": exit_code,
        "child_returncode": child_rc,
        "result_seen": state.result_seen,
        "result_is_error": state.result_is_error,
        "events": state.events,
        "idle_seconds": round(idle_seconds, 1),
        "pending_tools": pending_at_trip,
        "escalated_to_sigkill": escalated,
        "duration_seconds": round(time.monotonic() - started, 1),
    }
    line = json.dumps(verdict, sort_keys=True)
    if verdict_file:
        try:
            with open(verdict_file, "a") as f:
                f.write(line + "\n")
        except OSError as exc:
            _log(f"could not write verdict file {verdict_file}: {exc}", err)
    _log(f"verdict {line}", err)
    return exit_code, verdict


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(
            f"{LOG_PREFIX} invalid {ENV_PREFIX}{name}={raw!r}: expected seconds",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_USAGE) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gptme-claude-watchdog",
        description=(
            "Run a `claude -p --output-format stream-json --verbose` session and "
            "stop it when it hangs. Pass the session command after `--`."
        ),
        epilog=(
            "Defaults can also be set with AGENT_CLAUDE_WATCHDOG_IDLE_FREE, "
            "_IDLE_TOOLED, _GRACE, _KILL_AFTER and _POLL (seconds). "
            f"Exit {EXIT_HANG} means the session was stopped as hung."
        ),
    )
    parser.add_argument(
        "--idle-free",
        type=float,
        default=_env_float("IDLE_FREE", IDLE_FREE_DEFAULT),
        help="seconds without an event, with no tool pending, before stopping "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--idle-tooled",
        type=float,
        default=_env_float("IDLE_TOOLED", IDLE_TOOLED_DEFAULT),
        help="seconds without an event while a tool call is pending "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--grace",
        type=float,
        default=_env_float("GRACE", GRACE_DEFAULT),
        help="seconds the process may linger after its result event or EOF "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--kill-after",
        type=float,
        default=_env_float("KILL_AFTER", KILL_AFTER_DEFAULT),
        help="seconds between SIGTERM and SIGKILL (default: %(default)s)",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=_env_float("POLL", POLL_DEFAULT),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--verdict-file",
        default=os.environ.get(ENV_PREFIX + "VERDICT_FILE") or None,
        help="append the one-line JSON verdict to this file",
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="-- COMMAND ...")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.print_usage(sys.stderr)
        print(f"{LOG_PREFIX} missing session command after --", file=sys.stderr)
        return EXIT_USAGE
    for name in ("idle_free", "idle_tooled", "grace", "kill_after", "poll"):
        if getattr(args, name) <= 0:
            print(
                f"{LOG_PREFIX} --{name.replace('_', '-')} must be > 0", file=sys.stderr
            )
            return EXIT_USAGE
    config = WatchdogConfig(
        idle_free=args.idle_free,
        idle_tooled=args.idle_tooled,
        grace=args.grace,
        kill_after=args.kill_after,
        poll=args.poll,
    )
    try:
        exit_code, _ = run_watchdog(
            cmd,
            config,
            verdict_file=args.verdict_file,
            install_signal_handlers=True,
        )
    except FileNotFoundError as exc:
        print(f"{LOG_PREFIX} cannot start session: {exc}", file=sys.stderr)
        return 127
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
