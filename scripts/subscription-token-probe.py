#!/usr/bin/env python3
"""Live token-validity probe for inactive subscription slots.

Catches server-side token invalidation that mtime-based staleness and
offline expiresAt checks both miss. For every inactive slot with a
non-missing/non-malformed credential file, it makes ONE live API call
(count_tokens, ~$0, not inference) to verify the token actually works.

Background: in a 2026-06-02→03 credential-rot outage, a credential file
had a recent mtime and a valid-looking expiresAt, but the refresh token had
been rotated server-side. The offline check reported "ok" — a false green.
This probe prevents that.

Usage:
    # Probe all inactive slots (results to XDG state dir or GPTME_SUBSCRIPTION_STATE_DIR)
    ./scripts/subscription-token-probe.py

    # Write results to a specific path
    ./scripts/subscription-token-probe.py --output path/to/file.json

    # Print compact context snippet (reads last results; does not probe)
    ./scripts/subscription-token-probe.py --context

Configuration (via env vars):
    GPTME_SUBSCRIPTION_SLOTS           comma-separated slot names (default: bob,alice,erik)
    GPTME_SUBSCRIPTION_STATE_DIR       directory for state file (default: XDG_STATE_HOME/gptme-subscription)
    GPTME_SUBSCRIPTION_DEAD_SLOT_DIR   directory for TOKEN-DEAD marker files (optional)
    GPTME_SUBSCRIPTION_USAGE_SCRIPT    path to a check-claude-usage.sh-style probe (optional)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gptme_subscription.auth import check_credential_file, probe_credential

# ---- Configurable paths ----

CREDS_DIR = Path.home() / ".claude"

_state_dir_env = os.environ.get("GPTME_SUBSCRIPTION_STATE_DIR", "")
_xdg_state = (
    Path(os.environ.get("XDG_STATE_HOME", "")) or Path.home() / ".local" / "state"
)
_STATE_DIR = (
    Path(_state_dir_env) if _state_dir_env else _xdg_state / "gptme-subscription"
)
DEFAULT_OUTPUT = _STATE_DIR / "token-health.json"

# Dead-slot marker directory (optional; set GPTME_SUBSCRIPTION_DEAD_SLOT_DIR to enable)
_dead_slot_dir_env = os.environ.get("GPTME_SUBSCRIPTION_DEAD_SLOT_DIR", "")
DEAD_SLOT_DIR: Path | None = Path(_dead_slot_dir_env) if _dead_slot_dir_env else None

# Optional usage script for refresh-aware probing
_usage_script_env = os.environ.get("GPTME_SUBSCRIPTION_USAGE_SCRIPT", "")
USAGE_SCRIPT: Path | None = Path(_usage_script_env) if _usage_script_env else None

# Minimal payload for count_tokens — costs effectively $0, not inference
_COUNT_TOKENS_BODY = (
    b'{"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "x"}]}'
)
_API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
_REQUEST_TIMEOUT = 75  # seconds


def _default_slots() -> list[str]:
    """Return slots from env var or default to bob/alice/erik."""
    raw = os.environ.get("GPTME_SUBSCRIPTION_SLOTS", "")
    parsed = [s.strip() for s in raw.split(",") if s.strip()]
    return parsed or ["bob", "alice", "erik"]


def _active_slot() -> str | None:
    """Detect which slot the live symlink points to."""
    live = CREDS_DIR / ".credentials.json"
    try:
        target = live.resolve().name
    except OSError:
        return None
    return (
        target.rsplit(".", 1)[-1] if target.startswith(".credentials.json.") else None
    )


def _slot_path(slot: str) -> Path:
    return CREDS_DIR / f".credentials.json.{slot}"


def _offline_probe_error(slot: str) -> dict[str, Any] | None:
    """Return an error result for credentials that cannot be probed offline.

    A lapsed ``expiresAt`` only means the access token is stale; Claude Code can
    refresh it through the refresh token on next use. Do not reject stale tokens
    here. Only missing/malformed/unreadable credential files are terminal before
    the live probe.
    """
    info = check_credential_file(_slot_path(slot), slot)
    if info.status in ("valid", "stale"):
        return None
    return {
        "slot": slot,
        "status": "error",
        "credential_status": info.status,
        "http_code": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error": info.error or f"cannot probe: {info.status}",
    }


def _probe_slot(slot: str, token: str) -> dict[str, Any]:
    """Make live API call to verify a slot's token is valid server-side.

    Returns a result dict with:
        slot, status (valid/dead/error), http_code, checked_at, error
    """
    result: dict[str, Any] = {
        "slot": slot,
        "status": "unknown",
        "http_code": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

    req = urllib.request.Request(
        _API_URL,
        method="POST",
        data=_COUNT_TOKENS_BODY,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            result["http_code"] = resp.status
            if resp.status == 200:
                result["status"] = "valid"
            else:
                result["status"] = "dead"
    except urllib.error.HTTPError as e:
        result["http_code"] = e.code
        if e.code in (401, 403):
            result["status"] = "dead"
            body_preview = e.read().decode(errors="replace")[:200]
            result["error"] = f"HTTP {e.code}: {body_preview}"
        elif e.code == 429:
            result["status"] = "error"
            result["error"] = "rate_limited"
        else:
            result["status"] = "error"
            result["error"] = f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def _probe_slot_with_refresh(slot: str) -> dict[str, Any]:
    """Probe a slot through the same refresh-aware path as --check-auth --probe.

    Works even when the stored accessToken is stale/missing — the refresh token
    flow can still succeed server-side.
    """
    info, probe_ok, probe_msg = probe_credential(
        _slot_path(slot),
        slot,
        usage_script=USAGE_SCRIPT if USAGE_SCRIPT and USAGE_SCRIPT.exists() else None,
        timeout=_REQUEST_TIMEOUT,
    )
    result: dict[str, Any] = {
        "slot": slot,
        "status": "valid" if probe_ok else "dead",
        "credential_status": info.status,
        "expires_in_seconds": info.expires_in_seconds,
        "subscription_type": info.subscription_type,
        "http_code": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error": None if probe_ok else probe_msg,
        "probe_message": probe_msg,
    }
    if info.status in ("missing", "malformed", "unreadable"):
        result["status"] = "error"
    return result


def _write_dead_markers(results: list[dict[str, Any]]) -> None:
    """Write/remove TOKEN-DEAD file markers in DEAD_SLOT_DIR (if configured).

    Creates ``slot-TOKEN-DEAD-<slot>`` files for dead slots, removes them for
    slots that recovered. Agents can read these to surface dead-token warnings.
    """
    if DEAD_SLOT_DIR is None:
        return

    DEAD_SLOT_DIR.mkdir(parents=True, exist_ok=True)

    # Remove all existing TOKEN-DEAD markers first (clean slate)
    for entry in list(DEAD_SLOT_DIR.iterdir()):
        if entry.name.startswith("slot-TOKEN-DEAD-"):
            entry.unlink()

    # Write markers for currently dead slots
    for r in results:
        if r.get("status") == "dead":
            marker = DEAD_SLOT_DIR / f"slot-TOKEN-DEAD-{r['slot']}"
            marker.write_text(f"Token server-side dead at {r.get('checked_at', '?')}\n")


def probe_all(
    slots: list[str] | None = None, output: Path | None = None
) -> list[dict[str, Any]]:
    """Probe all inactive subscription slots and save results.

    Args:
        slots: List of slot names to probe. If None, detect from env/default.
        output: Path to write results. If None, use DEFAULT_OUTPUT.

    Returns:
        List of probe result dicts (one per inactive slot).
    """
    if slots is None:
        slots = _default_slots()

    active = _active_slot()
    results: list[dict[str, Any]] = []

    for slot in slots:
        if slot == active:
            # Skip the active slot — we know it works (this script runs on it)
            continue

        offline_error = _offline_probe_error(slot)
        if offline_error is not None:
            results.append(offline_error)
            continue

        result = _probe_slot_with_refresh(slot)
        results.append(result)

    # Preserve existing results for slots we didn't probe this cycle,
    # so the state file always has complete data
    existing: dict[str, dict[str, Any]] = {}
    if output and output.exists():
        try:
            existing_data = json.loads(output.read_text())
            existing_records = existing_data if isinstance(existing_data, list) else []
            for r in existing_records:
                if isinstance(r, dict):
                    existing[r.get("slot", "")] = r
        except (OSError, json.JSONDecodeError):
            pass

    merged = {r["slot"]: r for r in results}
    for slot_name, record in existing.items():
        if slot_name not in merged:
            merged[slot_name] = record

    merged_list = sorted(merged.values(), key=lambda r: r.get("slot", ""))

    # Write TOKEN-DEAD markers if a dead-slot dir is configured
    _write_dead_markers(merged_list)

    # Write JSON state file
    out_path = output or DEFAULT_OUTPUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(merged_list, indent=2) + "\n")

    return merged_list


def load_results(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the latest probe results from the state file."""
    p = path or DEFAULT_OUTPUT
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def has_dead_slots(results: list[dict[str, Any]]) -> bool:
    """Check if any slot has a dead token."""
    return any(r.get("status") == "dead" for r in results)


def dead_slot_names(results: list[dict[str, Any]]) -> list[str]:
    """Return names of slots with dead tokens."""
    return [r["slot"] for r in results if r.get("status") == "dead"]


def format_context(results: list[dict[str, Any]]) -> str:
    """Format probe results as a compact context snippet."""
    if not results:
        return "token-probe: no data"
    parts = []
    for r in sorted(results, key=lambda x: x.get("slot", "")):
        status = r.get("status", "unknown")
        slot = r.get("slot", "?")
        at = r.get("checked_at", "")[:16]  # YYYY-MM-DDTHH:MM
        if status == "valid":
            parts.append(f"{slot}=ok")
        elif status == "dead":
            parts.append(f"{slot}=DEAD({at})")
        else:
            parts.append(f"{slot}={status}({at})")
    return "token-probe: " + ", ".join(parts)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe inactive subscription slots for live token validity"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help="Print compact context snippet and exit",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()

    if args.context:
        results = load_results(args.output)
        print(format_context(results))
        return 0

    results = probe_all(output=args.output)

    print(f"Probed {len(results)} inactive slot(s):")
    dead = []
    for r in results:
        status = r.get("status", "?")
        icon = {"valid": "✓", "dead": "✗", "error": "⚠"}.get(status, "?")
        print(f"  {r['slot']}: {icon} {status}")
        if r.get("error"):
            print(f"      error: {r['error']}")
        if status == "dead":
            dead.append(r)

    if dead:
        print(f"\n⚠ {len(dead)} dead slot(s): {', '.join(r['slot'] for r in dead)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
