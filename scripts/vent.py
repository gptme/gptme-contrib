"""Cross-harness friction signal tool.

Appends a real-time friction entry to the shared ledger at
~/.local/share/gptme/friction-ledger.jsonl — the same file the native
gptme vent tool writes to, so all harnesses (gptme, Claude Code, Codex,
gptme-web) share a unified friction history.

Call this when stuck, frustrated, or hitting a repeated failure.
Rate-limited to one vent per 60 seconds (per workspace path) to prevent
recursive-venting spirals (Lovable hit a 43-vent death-spiral without this).
Identical messages repeated within 10 minutes are also suppressed (exit 3) so a
retrying gate doesn't inflate the ledger with duplicate rows that distort
friction analysis.

Usage:
    python3 scripts/vent.py "message describing friction" [--resolution-owner OWNER]
    python3 scripts/vent.py "message" --type Type0   # deprecated, use --resolution-owner

Resolution owners (axis 1 — who/what unblocks this):
    self          Solvable now with better prompting / context / reasoning
    tooling       Needs a tool / permission / config / env change
    operator      Needs Erik: a decision, credential, approval, account action
    upstream      Needs a fix in a dependency Bob doesn't own
    architectural Not solvable in the current stack design

--type is kept as a deprecated alias (Type1->self, Type2a->tooling, Type2b->architectural).
Type0 is Bob's extension, mapping to operator.

For Axis 2 (theme/cause) see analysis-time clustering in
packages/metaproductivity/src/metaproductivity/friction.py.

Examples:
    python3 scripts/vent.py "pytest exits 0 but finds no tests"
    python3 scripts/vent.py "OAuth token expired, need Erik to re-login" --resolution-owner operator
    python3 scripts/vent.py "missing API key prevents smoke test" --resolution-owner tooling
    python3 scripts/vent.py "tool output too large for context window" --resolution-owner architectural
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path.home() / ".local" / "share" / "gptme" / "friction-ledger.jsonl"
RATE_LIMIT_SECONDS = 60
# Suppress an *identical* message repeated within this window. The 60s rate
# limit is time-based and content-blind, so a gate or loop that re-fires the
# same blocker every retry (>60s apart) lands N identical ledger rows and looks
# like a recurring pattern to friction analysis. Content-dedup is purely
# additive: it can only drop duplicates, never widen the anti-spiral gate.
DEDUP_WINDOW_SECONDS = 600
# State file per workspace to track last-vent timestamp
_STATE_DIR = Path.home() / ".local" / "share" / "gptme"

# Deprecated type -> resolution_owner mapping
_DEPRECATED_TYPE_MAP = {
    "Type0": "operator",
    "Type1": "self",
    "Type2a": "tooling",
    "Type2b": "architectural",
}


def _detect_session_id(env: Mapping[str, str] | None = None) -> str | None:
    """Best-effort session ID extraction for cross-harness vent events."""
    source = env or os.environ
    # Claude Code: CC_SESSION_ID is the authoritative session identifier
    cc_id = source.get("CC_SESSION_ID", "").strip()
    if cc_id:
        return cc_id
    # Codex: CODEX_THREAD_ID is the thread/session identifier
    codex_id = source.get("CODEX_THREAD_ID", "").strip()
    if codex_id:
        return codex_id
    # gptme: GPTME_CONVERSATION_ID or GPTME_SESSION_ID
    for key in ("GPTME_CONVERSATION_ID", "GPTME_SESSION_ID"):
        val = source.get(key, "").strip()
        if val:
            return val
    return None


def _detect_harness(env: Mapping[str, str] | None = None) -> str:
    """Best-effort harness attribution for cross-harness vent events."""
    source = env or os.environ

    explicit = source.get("BOB_AMBIENT_HARNESS", "").strip()
    if explicit:
        return explicit

    backend = source.get("CONTRACT_DIAGNOSTICS_RUNTIME_BACKEND", "").strip()
    if backend:
        return backend

    if source.get("CLAUDECODE") or source.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"

    if (
        source.get("CODEX_THREAD_ID")
        or source.get("CODEX_CI")
        or source.get("CODEX_MANAGED_BY_NPM")
    ):
        return "codex"

    gptme_backend = source.get("GPTME_BACKEND", "").strip()
    if gptme_backend:
        return f"gptme:{gptme_backend}"

    return "unknown"


def _last_vent_path(workspace: Path) -> Path:
    slug = str(workspace).replace("/", "_").lstrip("_")
    return _STATE_DIR / f"vent-last-{slug}.txt"


def _check_rate_limit(workspace: Path) -> tuple[bool, float]:
    """Return (allowed, seconds_since_last). allowed=False means rate-limited."""
    state_file = _last_vent_path(workspace)
    if not state_file.exists():
        return True, 999.0
    try:
        last = float(state_file.read_text().strip())
    except (ValueError, OSError):
        return True, 999.0
    elapsed = time.time() - last
    return elapsed >= RATE_LIMIT_SECONDS, elapsed


def _record_timestamp(workspace: Path) -> None:
    state_file = _last_vent_path(workspace)
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file.write_text(str(time.time()))


def _is_duplicate(message: str, now: datetime) -> bool:
    """True if an identical message was recorded within DEDUP_WINDOW_SECONDS.

    Deduping against the ledger tail (the source of truth, so no extra state to
    prune) keeps friction analysis honest: a retrying gate would otherwise
    inflate the ledger with identical rows. Entries are append-ordered, so once
    we scan past the window we can stop.
    """
    if DEDUP_WINDOW_SECONDS <= 0 or not LEDGER_PATH.exists():
        return False
    cutoff = now.timestamp() - DEDUP_WINDOW_SECONDS
    try:
        lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines[-200:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = entry.get("timestamp")
        if not isinstance(ts, str):
            continue
        try:
            entry_dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        if entry_dt.timestamp() < cutoff:
            break  # older than the window — nothing earlier can match
        if entry.get("message") == message:
            return True
    return False


def vent(
    message: str,
    resolution_owner: str | None = None,
    friction_type: str | None = None,
    workspace: Path | None = None,
) -> None:
    if not message.strip():
        print("vent: no message — nothing recorded.", file=sys.stderr)
        sys.exit(1)

    ws = workspace or Path.cwd()
    msg = message.strip()
    now = datetime.now(timezone.utc)

    allowed, elapsed = _check_rate_limit(ws)
    if not allowed:
        wait = RATE_LIMIT_SECONDS - elapsed
        print(
            f"vent: rate-limited — last vent was {elapsed:.0f}s ago "
            f"(min {RATE_LIMIT_SECONDS}s). Retry in {wait:.0f}s.",
            file=sys.stderr,
        )
        sys.exit(2)

    if _is_duplicate(msg, now):
        print(
            f"vent: duplicate suppressed — identical message already recorded "
            f"within {DEDUP_WINDOW_SECONDS}s.",
            file=sys.stderr,
        )
        sys.exit(3)

    # Resolve resolution_owner: prefer --resolution-owner, fall back to deprecated --type
    final_owner = resolution_owner
    if friction_type:
        mapped = _DEPRECATED_TYPE_MAP.get(friction_type)
        if resolution_owner:
            print(
                f"vent: warning: both --resolution-owner ({resolution_owner}) and "
                f"--type ({friction_type}) provided — using --resolution-owner.",
                file=sys.stderr,
            )
        else:
            final_owner = mapped
            print(
                f"vent: warning: --type is deprecated, use --resolution-owner. "
                f"Mapped {friction_type} -> {mapped}.",
                file=sys.stderr,
            )

    entry: dict = {
        "timestamp": now.isoformat(),
        "workspace": str(ws),
        "message": msg,
    }
    if final_owner:
        entry["resolution_owner"] = final_owner
    if friction_type:
        entry["type"] = friction_type
    entry["harness"] = _detect_harness()
    session_id = _detect_session_id()
    if session_id:
        entry["session_id"] = session_id

    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    _record_timestamp(ws)
    print(f"Friction signal recorded to {LEDGER_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register a real-time friction signal (cross-harness vent tool)"
    )
    parser.add_argument("message", help="Brief description of the friction / blocker")
    parser.add_argument(
        "--resolution-owner",
        choices=["self", "tooling", "operator", "upstream", "architectural"],
        help="Who or what unblocks this: self=prompting/context, tooling=config/env, "
        "operator=Erik/human, upstream=dependency fix, architectural=redesign needed",
    )
    parser.add_argument(
        "--type",
        choices=["Type0", "Type1", "Type2a", "Type2b"],
        help="[DEPRECATED] Use --resolution-owner instead. "
        "Type1->self, Type2a->tooling, Type2b->architectural. Type0 maps -> operator.",
    )
    args = parser.parse_args()
    vent(
        args.message,
        resolution_owner=args.resolution_owner,
        friction_type=args.type,
    )


if __name__ == "__main__":
    main()
