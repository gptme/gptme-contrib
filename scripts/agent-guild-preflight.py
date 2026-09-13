#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Explicit, bounded endpoint observations; see docs/agent-guild-preflight.md."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ORIGIN = "https://agent-guild-5d5r.onrender.com"
MAX_URL_BYTES = 2048
MAX_BODY_BYTES = 96 * 1024
WORKER_SECONDS = 15
CHECKS = (
    "endpoint_reachable",
    "protocol_handshake",
    "agent_card_resolves",
    "agent_card_signed",
    "payment_claim_holds",
    "independent_evidence",
)
LIMITS = (
    "Observations are advisory and never authorize delegation or another call.",
    "Unknowns are excluded from the verdict, not counted as passes.",
    "Card signature presence is not cryptographic verification or endpoint ownership.",
    "payment_claim_holds observes a discovery-root HTTP 402, not payment or settlement.",
    "No paid task execution, safety, competence, data-handling or owner identity is proven.",
    "The Guild actively probes and logs the complete selected URL; use public non-secret URLs only.",
)


class Unavailable(ValueError):
    """A fixed local diagnostic, with no remote prose in its message."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow Guild HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(target: str) -> str:
    """Screen obvious non-public or ambiguous URLs without resolving the target."""
    try:
        if (
            not isinstance(target, str)
            or not 1 <= len(target.encode("utf-8")) <= MAX_URL_BYTES
        ):
            raise ValueError
        if (
            any(ord(c) <= 32 or ord(c) == 127 for c in target)
            or "\\" in target
            or "#" in target
        ):
            raise ValueError
        parts = urllib.parse.urlsplit(target)
        host = parts.hostname or ""
        if (
            parts.scheme not in ("http", "https")
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError
        if not host or not host.isascii() or "%" in host or host.endswith("."):
            raise ValueError
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.rsplit(".", 1)[-1].isdigit():
                raise ValueError
            if host.endswith(
                (
                    ".localhost",
                    ".local",
                    ".internal",
                    ".lan",
                    ".home",
                    ".test",
                    ".invalid",
                )
            ):
                raise ValueError
            if not all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", p)
                for p in host.split(".")
            ):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
        return target
    except (ValueError, UnicodeError) as error:
        raise Unavailable("invalid_public_url") from error


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys at every nesting level."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise Unavailable("invalid_response")
        value[key] = item
    return value


def no_constant(value: str) -> None:
    """Reject non-JSON numeric constants accepted by Python's default decoder."""
    raise Unavailable("invalid_response")


def name_set(value: Any) -> set[str]:
    """Validate a duplicate-free array containing only native check names."""
    if not isinstance(value, list) or any(
        not isinstance(n, str) or n not in CHECKS for n in value
    ):
        raise Unavailable("invalid_response")
    if len(set(value)) != len(value):
        raise Unavailable("invalid_response")
    return set(value)


def project(raw: bytes, target: str) -> dict[str, Any]:
    """Bind the original bounded JSON to the target and project fixed fields only."""
    validate_url(target)
    if len(raw) > MAX_BODY_BYTES:
        raise Unavailable("response_too_large")
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=no_constant,
        )
        if not isinstance(data, dict) or data.get("target") != target:
            raise Unavailable("target_mismatch")
        rows = data.get("checks")
        if not isinstance(rows, list) or len(rows) != len(CHECKS):
            raise Unavailable("invalid_response")
        statuses: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise Unavailable("invalid_response")
            name, status = row.get("check"), row.get("status")
            if not isinstance(name, str) or name not in CHECKS or name in statuses:
                raise Unavailable("invalid_response")
            if status not in ("proven", "failed", "unknown"):
                raise Unavailable("invalid_response")
            statuses[name] = status
        failed = {n for n in CHECKS if statuses[n] == "failed"}
        unknown = {n for n in CHECKS if statuses[n] == "unknown"}
        scored = set(CHECKS) - unknown
        if (
            name_set(data.get("failed")) != failed
            or name_set(data.get("unknowns")) != unknown
            or name_set(data.get("scored")) != scored
        ):
            raise Unavailable("inconsistent_response")
        verdict = (
            "do_not_delegate"
            if failed.intersection(CHECKS[:2])
            else "delegate_with_caution"
            if failed
            else "no_failed_checks"
        )
        if data.get("verdict") != verdict:
            raise Unavailable("inconsistent_response")
        return {
            "status": "observed",
            "target": target,
            "source": ORIGIN + "/preflight",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "response_bytes": len(raw),
            "checks": [{"check": n, "status": statuses[n]} for n in CHECKS],
            "failed": [n for n in CHECKS if n in failed],
            "unknowns": [n for n in CHECKS if n in unknown],
            "scored": [n for n in CHECKS if n in scored],
            "verdict": verdict,
            "delegation_authorized": False,
            "limits": list(LIMITS),
        }
    except Unavailable:
        raise
    except (UnicodeError, ValueError, RecursionError, TypeError) as error:
        raise Unavailable("invalid_response") from error


def observe(target: str) -> dict[str, Any]:
    """Worker operation; the public CLI supplies the outer process deadline."""
    validate_url(target)
    request = urllib.request.Request(
        ORIGIN + "/preflight?" + urllib.parse.urlencode({"url": target}),
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "gptme-agent-guild-preflight/0.1",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise Unavailable("http_status")
            if response.headers.get_content_type() != "application/json":
                raise Unavailable("unexpected_content_type")
            if (
                response.headers.get("Content-Encoding", "identity").lower()
                != "identity"
            ):
                raise Unavailable("unexpected_content_encoding")
            raw = response.read(MAX_BODY_BYTES + 1)
        return project(raw, target)
    except urllib.error.HTTPError as error:
        error.close()
        raise Unavailable("http_status") from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise Unavailable("transport_error") from error


def unavailable(code: str) -> dict[str, Any]:
    """Return a fixed local failure without promoting unavailable evidence."""
    return {
        "status": "unavailable",
        "reason": code,
        "delegation_authorized": False,
        "limits": list(LIMITS),
    }


def bounded_observe(target: str) -> dict[str, Any]:
    """Run one worker, killing and waiting for it if its 15-second budget expires."""
    validate_url(target)
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
            input=target.encode("utf-8"),
            capture_output=True,
            timeout=WORKER_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return unavailable("deadline_exceeded")
    if result.returncode or len(result.stdout) > MAX_BODY_BYTES:
        return unavailable("worker_failed")
    try:
        decoded = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        return unavailable("worker_failed")
    if (
        not isinstance(decoded, dict)
        or decoded.get("status") not in ("observed", "unavailable")
        or decoded.get("delegation_authorized") is not False
    ):
        return unavailable("worker_failed")
    return decoded


def main() -> int:
    """Execute an explicitly selected URL; no import-time network or configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url", nargs="?", help="Explicitly selected public non-secret HTTP(S) endpoint"
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            target = sys.stdin.buffer.read(MAX_URL_BYTES + 1).decode("utf-8")
            result = observe(target)
        elif args.url:
            result = bounded_observe(args.url)
        else:
            result = unavailable("missing_public_url")
    except Unavailable as error:
        result = unavailable(str(error))
    except (UnicodeError, OSError):
        result = unavailable("local_error")
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if args.worker or result["status"] == "observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
