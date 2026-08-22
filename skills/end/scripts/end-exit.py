#!/usr/bin/env python3
"""Exit the harness that is running this session (the `/end` skill's last step).

Finds the interactive harness process (Claude Code, gptme, Codex) that owns
this shell — via CLAUDE_PID or the /proc parent chain — and schedules a
SIGTERM to it from a detached helper, so the tool call that launched us
returns cleanly before the process goes away.

    end-exit.py --dry-run     # show which process would be terminated
    end-exit.py               # terminate after a 3s grace period
    end-exit.py --delay 10    # longer grace period
    end-exit.py --pid 12345   # override discovery (refuses pid 1 / self)

Caveats (honest ones):
  * Claude Code exits with status 143 on SIGTERM (verified 2.1.239). Session
    state is already on disk; `claude --resume` still works.
  * gptme's `/exit` runs SESSION_END hooks; SIGTERM does not. Conversation
    logs are written per message, so nothing is lost, but hook side effects
    (e.g. session-end summaries) are skipped. Prefer the native `/exit` when
    a human is at the keyboard.
  * Non-interactive runs (`claude -p`, `gptme -n`, `codex exec`) exit on their
    own when the turn ends — this script is a no-op there by design
    (`--if-interactive` is the default).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_check_module():
    spec = importlib.util.spec_from_file_location("end_check", HERE / "end-check.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module importable
    spec.loader.exec_module(mod)
    return mod


def _is_interactive(pid: int) -> bool:
    """Best-effort: does the harness own a terminal?"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        tty_nr = int(stat.rsplit(")", 1)[1].split()[4])
        return tty_nr != 0
    except (OSError, ValueError, IndexError):
        return True  # unknown → assume interactive (safer: don't silently skip)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--delay", type=float, default=3.0, help="grace period in seconds (default 3)"
    )
    ap.add_argument("--pid", type=int, help="override harness pid discovery")
    ap.add_argument("--signal", default="TERM", choices=["TERM", "INT", "HUP"])
    ap.add_argument(
        "--even-if-noninteractive",
        action="store_true",
        help="send the signal even when the harness has no controlling tty",
    )
    args = ap.parse_args(argv)

    check = _load_check_module()
    h = check.find_harness()
    pid = args.pid or h.pid
    if not pid:
        print(
            f"end-exit: no harness process found (detected: {h.name}); nothing to do",
            file=sys.stderr,
        )
        return 1
    if pid <= 0 or pid == 1 or pid in (os.getpid(), os.getppid()):
        print(f"end-exit: refusing to signal pid {pid}", file=sys.stderr)
        return 1
    cmd = " ".join(check._proc_cmdline(pid))[:160]
    interactive = _is_interactive(pid)
    print(f"harness: {h.name} pid={pid} interactive={interactive}")
    print(f"cmdline: {cmd}")
    if not interactive and not args.even_if_noninteractive:
        print(
            "non-interactive run — it exits on its own when this turn ends; not signalling."
        )
        return 0
    if args.dry_run:
        print(f"dry-run: would send SIG{args.signal} in {args.delay:g}s")
        return 0
    helper = f"sleep {args.delay:g}; kill -{args.signal} {pid}"
    subprocess.Popen(
        ["sh", "-c", helper],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(
        f"SIG{args.signal} scheduled for pid {pid} in {args.delay:g}s — session ending."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
