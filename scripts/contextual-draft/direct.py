#!/usr/bin/env python3
"""Hotkey action: capture, draft from context, insert into the unchanged target.

Invocation authorizes model disclosure and insertion, never sending. Native target
stays alive through generation. No UI activation, clipboard or keystroke fallback.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import selectors
import subprocess
import sys
import tempfile
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


def read_line(proc: subprocess.Popen, timeout: int) -> bytes:
    assert proc.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise core.DraftError("Native target session timed out")
    line: bytes = proc.stdout.readline(100001)
    if not line or len(line) > 100000:
        raise core.DraftError("Native target unavailable; no insertion attempted")
    return line


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
        )
    except OSError:
        return None


def stop_progress(progress: subprocess.Popen | None) -> None:
    if progress is None:
        return
    if progress.stdin is not None:
        progress.stdin.close()
    try:
        progress.wait(timeout=3)
    except subprocess.TimeoutExpired:
        progress.terminate()
        progress.wait(timeout=3)


def execute(args: argparse.Namespace) -> None:
    directory = Path(tempfile.mkdtemp(prefix="contextual-draft-"))
    source = Path(__file__).with_name("capture.swift")
    native = subprocess.Popen(
        ["swift", str(source), "--insert-session"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    progress = None
    try:
        target = core.validate_target(json.loads(read_line(native, 30)))
        if (target.get("selection") or {}).get("length") != 0:
            raise core.DraftError(
                "Direct draft needs a cursor, not a selection; use preview"
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
        output = directory / "draft.txt"
        core.generate(
            argparse.Namespace(
                allow_model_disclosure=True,
                request=request,
                model=args.model,
                output=output,
            )
        )
        draft = core.read_bounded(output, 100000).decode("utf-8").strip()
        if not draft:
            raise core.DraftError("Insufficient context; no draft inserted")
        assert native.stdin is not None
        native.stdin.write(
            (json.dumps({"draft": draft}, ensure_ascii=False) + "\n").encode("utf-8")
        )
        native.stdin.flush()
        reply = json.loads(read_line(native, 10))
        if reply.get("inserted") is not True:
            raise core.DraftError("Insertion not confirmed; draft retained privately")
        print("Draft inserted. Nothing sent.")
    except (OSError, ValueError) as exc:
        raise core.DraftError(
            "Direct drafting failed; private request/draft retained, no fallback insertion"
        ) from exc
    finally:
        stop_progress(progress)
        if native.poll() is None:
            native.terminate()
        try:
            native.wait(timeout=5)
        except subprocess.TimeoutExpired:
            native.kill()
            native.wait()


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
        subprocess.run(
            [
                "/opt/homebrew/bin/terminal-notifier",
                "-title",
                "Contextual draft",
                "-message",
                "Draft not inserted. Check the private launcher log or use preview.",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
