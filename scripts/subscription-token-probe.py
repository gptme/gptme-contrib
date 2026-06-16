#!/usr/bin/env -S uv run python3
"""Live token-validity probe for inactive subscription slots.

Catches server-side token invalidation that mtime-based staleness and offline
expiresAt checks both miss.  In the 2026-06-02→03 credential-rot outage, a
slot's credential file had a recent mtime and a valid-looking expiresAt, but
the refresh token had been rotated server-side.  The offline check reported a
false green.  This probe prevents that: for every inactive slot with a
non-terminal credential file, it makes ONE live call through the
refresh-aware probe path to confirm the token actually works.

Results are written to a configurable state file and optionally accompanied
by TOKEN-DEAD marker files consumable by vitals scripts.

Configuration (priority: CLI arg > env var > default):
    Output path:     --output / GPTME_SUBSCRIPTION_HEALTH_OUTPUT
    Dead-slot dir:   --dead-slot-dir / GPTME_SUBSCRIPTION_DEAD_SLOT_DIR
    Usage script:    --usage-script / GPTME_CLAUDE_USAGE_SCRIPT
    Slot list:       --slots / GPTME_SUBSCRIPTION_SLOTS (comma-separated)

Usage:
    # Probe all inactive slots (using env vars for paths)
    ./scripts/subscription-token-probe.py

    # Explicit paths
    ./scripts/subscription-token-probe.py \\
        --output ~/bob/state/subscription-token-health.json \\
        --dead-slot-dir ~/bob/state/backend-quota \\
        --usage-script ~/bob/scripts/check-claude-usage.sh

    # Print compact context snippet from last run
    ./scripts/subscription-token-probe.py --context
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

CREDS_DIR = Path.home() / ".claude"

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


def _read_slot_token(slot: str) -> str | None:
    """Read the accessToken from a slot's credential file."""
    p = _slot_path(slot)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None
        token = oauth.get("accessToken")
        return str(token) if token else None
    except (OSError, json.JSONDecodeError, KeyError):
        return None


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


def _probe_slot_with_refresh(
    slot: str, usage_script: Path | None = None
) -> dict[str, Any]:
    """Probe a slot through the same refresh-aware path as --check-auth --probe."""
    info, probe_ok, probe_msg = probe_credential(
        _slot_path(slot),
        slot,
        usage_script=usage_script,
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


def _write_dead_markers(
    results: list[dict[str, Any]], dead_slot_dir: Path | None = None
) -> None:
    """Write/remove TOKEN-DEAD file markers consumable by vitals scripts.

    Creates ``slot-TOKEN-DEAD-<slot>`` files for dead tokens and removes
    stale markers for slots that have recovered. Skips marker management
    when ``dead_slot_dir`` is None.
    """
    if dead_slot_dir is None:
        return

    dead_slot_dir.mkdir(parents=True, exist_ok=True)

    # Remove all existing TOKEN-DEAD markers first (clean slate)
    for entry in list(dead_slot_dir.iterdir()):
        if entry.name.startswith("slot-TOKEN-DEAD-"):
            entry.unlink()

    # Write markers for currently dead slots
    for r in results:
        if r.get("status") == "dead":
            marker = dead_slot_dir / f"slot-TOKEN-DEAD-{r['slot']}"
            marker.write_text(f"Token server-side dead at {r.get('checked_at', '?')}\n")


def probe_all(
    slots: list[str] | None = None,
    output: Path | None = None,
    dead_slot_dir: Path | None = None,
    usage_script: Path | None = None,
) -> list[dict[str, Any]]:
    """Probe all inactive subscription slots and save results.

    Args:
        slots: List of slot names to probe. If None, detect from env/default.
        output: Path to write results JSON. If None, results are returned but
            not written to disk and dead-slot markers are not updated.
        dead_slot_dir: Directory for TOKEN-DEAD marker files. If None,
            marker management is skipped.
        usage_script: Path to the check-claude-usage.sh script used for
            refresh-aware probing. If None, probes skip the refresh step.

    Returns:
        List of probe result dicts (one per inactive slot probed).
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

        result = _probe_slot_with_refresh(slot, usage_script=usage_script)
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

    # Write TOKEN-DEAD markers for vitals integration
    _write_dead_markers(merged_list, dead_slot_dir)

    # Write JSON state file
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(merged_list, indent=2) + "\n")

    return merged_list


def load_results(path: Path) -> list[dict[str, Any]]:
    """Load the latest probe results from a state file."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
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
        elif status == "expired_offline":
            parts.append(f"{slot}=expired({at})")
        elif status == "dead":
            parts.append(f"{slot}=DEAD({at})")
        else:
            parts.append(f"{slot}={status}({at})")
    return "token-probe: " + ", ".join(parts)


def _resolve_usage_script(arg: Path | None) -> Path | None:
    """Resolve the usage script path from arg, env var, or common locations."""
    if arg is not None:
        return arg if arg.exists() else None

    env_val = os.environ.get("GPTME_CLAUDE_USAGE_SCRIPT", "")
    if env_val:
        p = Path(env_val)
        return p if p.exists() else None

    # Auto-detect common agent workspace layouts
    for candidate in [
        Path.home() / "bob" / "scripts" / "check-claude-usage.sh",
        Path.home() / "alice" / "scripts" / "check-claude-usage.sh",
        Path.home() / "scripts" / "check-claude-usage.sh",
    ]:
        if candidate.exists():
            return candidate

    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_output = os.environ.get("GPTME_SUBSCRIPTION_HEALTH_OUTPUT", "")
    default_dead_dir = os.environ.get("GPTME_SUBSCRIPTION_DEAD_SLOT_DIR", "")
    parser = argparse.ArgumentParser(
        description="Probe inactive subscription slots for live token validity"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(default_output) if default_output else None,
        help=(
            "Output path for health JSON "
            "(env: GPTME_SUBSCRIPTION_HEALTH_OUTPUT; default: no output file)"
        ),
    )
    parser.add_argument(
        "--dead-slot-dir",
        type=Path,
        default=Path(default_dead_dir) if default_dead_dir else None,
        help=(
            "Directory for TOKEN-DEAD marker files "
            "(env: GPTME_SUBSCRIPTION_DEAD_SLOT_DIR; default: disabled)"
        ),
    )
    parser.add_argument(
        "--usage-script",
        type=Path,
        default=None,
        help=(
            "Path to check-claude-usage.sh for refresh-aware probing "
            "(env: GPTME_CLAUDE_USAGE_SCRIPT; auto-detected from common locations)"
        ),
    )
    parser.add_argument(
        "--slots",
        default=None,
        help="Comma-separated slot names (env: GPTME_SUBSCRIPTION_SLOTS; default: bob,alice,erik)",
    )
    parser.add_argument(
        "--context",
        action="store_true",
        help="Print compact context snippet from last run and exit",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()

    if args.context:
        if args.output is None:
            print("token-probe: no output path configured (use --output)")
            return 1
        results = load_results(args.output)
        print(format_context(results))
        return 0

    slots = (
        [s.strip() for s in args.slots.split(",") if s.strip()]
        if args.slots
        else _default_slots()
    )
    usage_script = _resolve_usage_script(args.usage_script)

    results = probe_all(
        slots=slots,
        output=args.output,
        dead_slot_dir=args.dead_slot_dir,
        usage_script=usage_script,
    )

    print(f"Probed {len(results)} inactive slot(s):")
    dead = []
    for r in results:
        status = r.get("status", "?")
        icon = {"valid": "✓", "dead": "✗", "expired_offline": "~", "error": "⚠"}.get(
            status, "?"
        )
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
