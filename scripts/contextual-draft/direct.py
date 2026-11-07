#!/usr/bin/env python3
"""Hotkey action: capture, draft from context, insert into the unchanged target.

Invocation authorizes model disclosure and insertion, never sending. Native target
stays alive through generation. No UI activation, clipboard or keystroke fallback.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from preview import core

DEFAULT_MODEL = "openai-subscription/gpt-6.1-sol"
INSTRUCTION = """Write the message that belongs in the focused field, as the user.
Infer the recipient, conversation, purpose and appropriate tone from the available
context. Use relevant observed facts and external agent context. Produce only the
message body to insert at the cursor, no preamble or quotation marks. Do not rewrite
selected words or ask what rewriting is desired. Preserve any existing draft intent
and produce only the continuation when there is already text. Do not invent facts,
payments, promises, or completed actions. If context is insufficient to identify a
useful message, output an empty body rather than a clarification in the recipient's
field. Never send or submit anything."""


MAX_LINE = core.MAX_PROTOCOL_LINE  # Derivation and worst cases: see core.
# The helper must finish insertion strictly inside the reply deadline: it aborts
# before its single AX write once INSERT_BUDGET has elapsed since it received
# the draft, and that write is bounded by its 2s AX messaging timeout.
INSERT_REPLY_TIMEOUT = 10.0
INSERT_BUDGET = INSERT_REPLY_TIMEOUT - 4.0


def read_line(proc: subprocess.Popen, timeout: float) -> bytes:
    """Read one complete protocol line against an overall deadline.

    A helper that writes a partial line and stays alive must not block forever
    while this process holds the exclusive drafting lock, so every read waits
    only for the time remaining. The protocol is strict request/response: bytes
    after the newline are a protocol error rather than a later message.
    """
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    line = b""
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while not line.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise core.DraftError("Native target session timed out")
            chunk = os.read(fd, MAX_LINE + 1 - len(line))
            if not chunk:
                raise core.DraftError(
                    "Native target unavailable; no insertion attempted"
                )
            line += chunk
            if len(line) > MAX_LINE or b"\n" in line[:-1]:
                raise core.DraftError(
                    "Native target unavailable; no insertion attempted"
                )
    return line


def write_line(proc: subprocess.Popen, line: bytes, timeout: float) -> None:
    """Write one protocol line against an overall deadline.

    A helper that stops reading must not block this process (and its exclusive
    drafting lock) on a full pipe, so each write waits only for the time left.
    """
    assert proc.stdin is not None
    fd = proc.stdin.fileno()
    deadline = time.monotonic() + timeout
    view = memoryview(line)
    os.set_blocking(fd, False)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE)
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise core.DraftError("Native target session timed out")
                try:
                    view = view[os.write(fd, view) :]
                except BlockingIOError:
                    continue
    finally:
        os.set_blocking(fd, True)


def run(args: argparse.Namespace) -> None:
    cache = Path.home() / ".cache" / "contextual-draft"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (cache / "direct.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise core.DraftError("A draft is already running") from exc
        execute(args)


def start_progress(target: dict) -> subprocess.Popen | None:
    """Best-effort non-activating HUD, coupled to this process by stdin EOF."""
    if sys.platform != "darwin":
        return None
    bounds = target.get("field_bounds") or {}
    command = ["swift", str(Path(__file__).with_name("progress.swift"))]
    if "x" in bounds and "y" in bounds:
        command.extend([str(bounds["x"]), str(bounds["y"])])
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Own group so cleanup also reaches the swift driver's frontend.
            start_new_session=True,
        )
    except OSError:
        return None


def stop_progress(progress: subprocess.Popen | None) -> None:
    if progress is None:
        return
    if progress.stdin is not None:
        progress.stdin.close()
    # Stdin EOF asks the HUD to exit; then stop its whole group, bounded.
    deadline = time.monotonic() + 3
    while not core.leader_exited(progress) and time.monotonic() < deadline:
        time.sleep(0.05)
    core.stop_group(progress, grace=3)


def execute(args: argparse.Namespace) -> None:
    root = (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "contextual-draft"
        / "runs"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    print(f"Private session: {directory}", flush=True)
    core.log_event(directory, "started", model=args.model)
    started = time.monotonic()
    source = Path(__file__).with_name("capture.swift")
    native_errors = (directory / "native-stderr.txt").open("xb")
    os.chmod(directory / "native-stderr.txt", 0o600)
    try:
        native = subprocess.Popen(
            ["swift", str(source), "--insert-session"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=native_errors,
            # Own group: cleanup must also stop the driver's frontend process.
            start_new_session=True,
        )
    except OSError as exc:
        native_errors.close()
        core.log_event(
            directory, "capture_launch_failed", error_type=type(exc).__name__
        )
        raise core.DraftError(
            f"Cannot start native capture; diagnostics: {directory}"
        ) from exc
    progress = None
    native_stopped = False
    try:
        target = core.validate_target(json.loads(read_line(native, 30)))
        if (target.get("selection") or {}).get("length") != 0:
            raise core.DraftError(
                "Direct draft needs a cursor, not a selection; use preview"
            )
        core.log_event(
            directory,
            "captured",
            app=target.get("bundle_id"),
            text_characters=len(target["text"]),
            elapsed_s=time.monotonic() - started,
        )
        progress = start_progress(target)
        target_file = directory / "target.json"
        core.private_write(target_file, json.dumps(target, ensure_ascii=False))
        request = directory / "request.json"
        core.prepare(
            argparse.Namespace(
                capture=False,
                target=target_file,
                context=args.context,
                goldfish_db=args.goldfish_db,
                query=args.query,
                limit=5,
                instruction=INSTRUCTION,
                output=request,
            )
        )
        prepared = core.read_json(request)
        core.log_event(
            directory,
            "prepared",
            source_ids=[r["snapshot_id"] for r in prepared["observations"]],
            reference_sources=[r["source"] for r in prepared["agent_context"]],
            budget_dropped_ids=prepared["coverage"]["budget"]["dropped_snapshot_ids"],
            budget_truncated_ids=prepared["coverage"]["budget"][
                "truncated_snapshot_ids"
            ],
            request_bytes=request.stat().st_size,
        )
        output = directory / "draft.txt"
        core.generate(
            argparse.Namespace(
                allow_model_disclosure=True,
                request=request,
                model=args.model,
                output=output,
            )
        )
        draft = core.read_bounded(output, core.MAX_DRAFT).decode("utf-8")
        if not draft.strip():
            raise core.DraftError("Insufficient context; no draft inserted")
        command = {"draft": draft, "budget_s": INSERT_BUDGET}
        try:
            write_line(
                native,
                (json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"),
                INSERT_REPLY_TIMEOUT,
            )
            reply = json.loads(read_line(native, INSERT_REPLY_TIMEOUT))
        except BaseException:
            # No confirmed outcome: kill the helper's group now, before any
            # other cleanup wait, so a late insertion cannot follow a reported
            # failure (and a retry cannot duplicate the draft).
            core.kill_group(native)
            native.wait()
            native_stopped = True
            raise
        if reply.get("inserted") is not True:
            raise core.DraftError("Insertion not confirmed; draft retained privately")
        core.log_event(directory, "inserted", elapsed_s=time.monotonic() - started)
        print("Draft inserted. Nothing sent.")
    except core.DraftError as exc:
        core.log_event(
            directory, "failed", reason=str(exc), elapsed_s=time.monotonic() - started
        )
        raise core.DraftError(
            f"{exc}; diagnostics and retained draft: {directory}"
        ) from exc
    except (OSError, ValueError) as exc:
        core.log_event(
            directory,
            "failed",
            error_type=type(exc).__name__,
            elapsed_s=time.monotonic() - started,
        )
        raise core.DraftError(
            f"Direct drafting failed; diagnostics and retained draft: {directory}; no fallback insertion"
        ) from exc
    finally:
        stop_progress(progress)
        native_errors.close()
        # The helper exits by itself after replying; otherwise stop its group.
        # A group already killed and reaped above is never signalled again.
        if not native_stopped:
            deadline = time.monotonic() + 5
            while not core.leader_exited(native) and time.monotonic() < deadline:
                time.sleep(0.05)
            core.stop_group(native, grace=5)
        for stream in (native.stdin, native.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--context", action="append", type=Path, default=[])
    parser.add_argument("--query")
    parser.add_argument(
        "--goldfish-db",
        type=Path,
        default=Path.home() / "Library/Application Support/goldfish/goldfish.db",
    )
    args = parser.parse_args()
    try:
        if sys.platform != "darwin":
            raise core.DraftError("Native direct drafting currently requires macOS")
        run(args)
    except core.DraftError as exc:
        print(f"contextual-draft: {exc}", file=sys.stderr)
        # Fixed notification text only: no captured contents or draft in arguments.
        notifier = shutil.which("terminal-notifier")
        if notifier is None:
            sys.exit(1)
        subprocess.run(
            [
                notifier,
                "-title",
                "Contextual draft",
                "-message",
                (
                    "No draft: the model abstained or returned an empty response. Use preview to inspect context."
                    if "returned no draft" in str(exc)
                    else "Draft failed before insertion. Use preview or inspect the private launcher log."
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
