"""Cross-agent voice handoff protocol (v1).

Library module for initiating and validating voice-call handoffs between agents
(Bob, Alice, Gordon, Sven). The protocol is specified in
knowledge/technical-designs/cross-agent-voice-handoff.md (in Bob's workspace).

This module contains the load-bearing primitives:

* ``compute_hmac`` / ``validate`` — HMAC-SHA256 over canonical JSON.
* ``build_handoff`` — construct and sign a v1 payload.
* ``atomic_write`` / ``atomic_move`` / ``make_state_dirs`` — filesystem
  primitives used by the state machine (handoff/ -> claimed/ -> archive/).
* ``HandoffWriter`` — convenience wrapper for the initiator side: signs a
  payload and publishes it to a shared state directory via atomic rename.

Phase 1 shipped a protocol spec, validator, and a dry-run harness living
in Bob's workspace (``scripts/voice-handoff-*.py``). This module consolidates
the primitives inside the ``gptme-voice`` package so downstream integrations
(voice-server write-side, target-agent listener) can depend on a typed,
unit-tested library instead of importing from a sibling script via
``importlib``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

PROTOCOL_VERSION = 1

REQUIRED_FIELDS: tuple[str, ...] = (
    "protocol_version",
    "handoff_id",
    "caller_hash",
    "from_agent",
    "to_agent",
    "reason",
    "initiated_at",
    "expires_at",
    "transcript",
    "hmac",
)

VALID_AGENTS: frozenset[str] = frozenset({"bob", "alice", "gordon", "sven"})

STATE_SUBDIRS: tuple[str, ...] = ("handoff", "claimed", "archive", "rejected")

_DEFAULT_TTL_SECONDS = 60


class ValidationResult(NamedTuple):
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _parse_iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _canonical_json(payload: dict[str, Any]) -> bytes:
    """Canonical JSON bytes for HMAC (sorted keys, no whitespace, hmac stripped)."""
    stripped = {k: v for k, v in payload.items() if k != "hmac"}
    return json.dumps(stripped, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_hmac(payload: dict[str, Any], secret: bytes) -> str:
    """HMAC-SHA256 over canonical JSON, base64-encoded."""
    mac = hmac.new(secret, _canonical_json(payload), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def validate(
    payload: dict[str, Any],
    *,
    secret: bytes | None = None,
    now: datetime | None = None,
) -> ValidationResult:
    """Validate a handoff payload against protocol v1.

    If ``secret`` is provided, HMAC is verified. If omitted, HMAC field presence is
    checked but its value is not verified (useful for draft validation before signing).
    """
    if not isinstance(payload, dict):
        return ValidationResult(False, "payload is not a JSON object")

    if "protocol_version" not in payload:
        return ValidationResult(False, "missing required field: protocol_version")
    version = payload["protocol_version"]
    if version != PROTOCOL_VERSION:
        return ValidationResult(
            False,
            f"unsupported protocol_version: {version} (expected {PROTOCOL_VERSION})",
        )

    for field in REQUIRED_FIELDS:
        if field not in payload:
            return ValidationResult(False, f"missing required field: {field}")

    for agent_field in ("from_agent", "to_agent"):
        if payload[agent_field] not in VALID_AGENTS:
            return ValidationResult(
                False,
                f"{agent_field}={payload[agent_field]!r} not in {sorted(VALID_AGENTS)}",
            )
    if payload["from_agent"] == payload["to_agent"]:
        return ValidationResult(False, "from_agent and to_agent must differ")

    payload_caller_hash = payload["caller_hash"]
    if (
        not isinstance(payload_caller_hash, str)
        or len(payload_caller_hash) != 16
        or not all(c in "0123456789abcdef" for c in payload_caller_hash)
    ):
        return ValidationResult(False, "caller_hash must be 16 lowercase hex chars")

    initiated = _parse_iso8601(payload["initiated_at"])
    expires = _parse_iso8601(payload["expires_at"])
    if initiated is None:
        return ValidationResult(False, "initiated_at is not valid ISO 8601")
    if expires is None:
        return ValidationResult(False, "expires_at is not valid ISO 8601")
    if expires <= initiated:
        return ValidationResult(False, "expires_at must be after initiated_at")

    current = now or datetime.now(timezone.utc)
    if current > expires:
        return ValidationResult(
            False,
            f"handoff expired at {expires.isoformat()} (now={current.isoformat()})",
        )

    transcript = payload["transcript"]
    if not isinstance(transcript, list):
        return ValidationResult(False, "transcript must be a list")
    for i, turn in enumerate(transcript):
        if not isinstance(turn, dict):
            return ValidationResult(False, f"transcript[{i}] is not an object")
        if turn.get("role") not in ("user", "assistant", "system"):
            return ValidationResult(
                False, f"transcript[{i}].role must be user/assistant/system"
            )
        if not isinstance(turn.get("text"), str):
            return ValidationResult(False, f"transcript[{i}].text must be a string")

    if secret is not None:
        expected = compute_hmac(payload, secret)
        if not hmac.compare_digest(expected, str(payload["hmac"])):
            return ValidationResult(
                False, "HMAC mismatch (payload tampered or wrong secret)"
            )

    return ValidationResult(True)


def caller_hash(caller_id: str) -> str:
    """Return the 16-char hex digest used to identify a caller without leaking PII."""
    return hashlib.sha256(caller_id.encode("utf-8")).hexdigest()[:16]


def build_handoff(
    *,
    from_agent: str,
    to_agent: str,
    caller_id: str,
    reason: str,
    secret: bytes,
    transcript: list[dict[str, Any]] | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct and sign a protocol-v1 handoff payload.

    ``transcript`` is the conversation history to carry across the handoff.
    Pass ``extra`` for optional fields (``context_summary``, ``pending_actions``,
    ``resume_hint``, etc.) that aren't required by the validator but are useful
    for the target agent.
    """
    if from_agent not in VALID_AGENTS:
        raise ValueError(f"from_agent={from_agent!r} not in {sorted(VALID_AGENTS)}")
    if to_agent not in VALID_AGENTS:
        raise ValueError(f"to_agent={to_agent!r} not in {sorted(VALID_AGENTS)}")
    if from_agent == to_agent:
        raise ValueError("from_agent and to_agent must differ")

    now = now or datetime.now(timezone.utc)
    ts = now.isoformat().replace("+00:00", "Z")
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    ch = caller_hash(caller_id)
    handoff_id = f"{int(now.timestamp())}-{from_agent}-{to_agent}-{ch}"

    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "handoff_id": handoff_id,
        "caller_hash": ch,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "reason": reason,
        "initiated_at": ts,
        "expires_at": expires,
        "transcript": list(transcript or []),
    }
    if extra:
        # Don't let extra fields overwrite required fields (the HMAC would still
        # cover them, but the schema would become surprising to readers).
        for key, value in extra.items():
            if key in payload or key == "hmac":
                raise ValueError(f"extra field {key!r} collides with protocol field")
            payload[key] = value
    payload["hmac"] = compute_hmac(payload, secret)
    return payload


def make_state_dirs(root: Path) -> dict[str, Path]:
    """Ensure ``handoff/``, ``claimed/``, ``archive/``, ``rejected/`` exist under ``root``."""
    dirs = {name: root / name for name in STATE_SUBDIRS}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a sibling tempfile + ``os.replace``.

    Readers polling the directory will never see a partially-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_move(src: Path, dst: Path) -> None:
    """Same-filesystem atomic rename (``os.replace``), creating ``dst.parent`` if needed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def _next_sequence(handoff_dir: Path, caller_digest: str) -> int:
    """Return the next monotonic sequence number for this caller in ``handoff_dir``.

    Looks at both live handoff files and any shadowed ``.NAME.tmp`` in-flight
    writes, so a racing second publish with the same timestamp doesn't reuse a
    filename.
    """
    max_seq = -1
    prefix = f"{caller_digest}-"
    for entry in handoff_dir.iterdir() if handoff_dir.exists() else ():
        name = entry.name.lstrip(".")
        if not name.startswith(prefix):
            continue
        # Accept both committed files ({hash}-{seq}.json) and in-flight atomics
        # ({hash}-{seq}.json.tmp — written by atomic_write before os.replace).
        if name.endswith(".json"):
            seq_str = name[len(prefix) : -len(".json")]
        elif name.endswith(".json.tmp"):
            seq_str = name[len(prefix) : -len(".json.tmp")]
        else:
            continue
        try:
            seq = int(seq_str)
        except ValueError:
            continue
        max_seq = max(max_seq, seq)
    return max_seq + 1


@dataclass
class PublishedHandoff:
    """Return value from :meth:`HandoffWriter.initiate`."""

    path: Path
    payload: dict[str, Any]


class HandoffWriter:
    """Initiator-side helper: sign a payload and publish it to ``handoff/``.

    The writer is stateless aside from the shared directory; a voice server can
    keep a single instance per peer and call :meth:`initiate` whenever the LLM
    decides to transfer the call.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        from_agent: str,
        secret: bytes,
    ) -> None:
        if from_agent not in VALID_AGENTS:
            raise ValueError(f"from_agent={from_agent!r} not in {sorted(VALID_AGENTS)}")
        if not secret:
            raise ValueError("secret must be non-empty bytes")
        self.state_dir = state_dir
        self.from_agent = from_agent
        self.secret = secret
        self._dirs = make_state_dirs(state_dir)

    @property
    def handoff_dir(self) -> Path:
        return self._dirs["handoff"]

    def initiate(
        self,
        *,
        to_agent: str,
        caller_id: str,
        reason: str,
        transcript: list[dict[str, Any]] | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        extra: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> PublishedHandoff:
        """Build a signed handoff and publish it atomically to ``handoff/``.

        Raises ``ValueError`` for schema violations (unknown agent, self-handoff).
        The returned ``PublishedHandoff`` carries both the final on-disk path and
        the signed payload, so callers can log or surface the ``handoff_id``
        without re-reading the file.
        """
        payload = build_handoff(
            from_agent=self.from_agent,
            to_agent=to_agent,
            caller_id=caller_id,
            reason=reason,
            secret=self.secret,
            transcript=transcript,
            ttl_seconds=ttl_seconds,
            now=now,
            extra=extra,
        )
        digest = payload["caller_hash"]
        seq = _next_sequence(self.handoff_dir, digest)
        path = self.handoff_dir / f"{digest}-{seq}.json"
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        atomic_write(path, data)
        return PublishedHandoff(path=path, payload=payload)


def archive_filename(
    payload: dict[str, Any], *, completed_at: float | None = None
) -> str:
    """Return the canonical ``archive/`` filename for a completed handoff.

    Filenames encode both agents plus the handoff id so lineage is preserved.
    """
    ts = int(completed_at if completed_at is not None else time.time())
    return f"{ts}-{payload['from_agent']}-{payload['to_agent']}-{payload['handoff_id']}.json"


__all__ = [
    "PROTOCOL_VERSION",
    "REQUIRED_FIELDS",
    "STATE_SUBDIRS",
    "VALID_AGENTS",
    "HandoffWriter",
    "PublishedHandoff",
    "ValidationResult",
    "archive_filename",
    "atomic_move",
    "atomic_write",
    "build_handoff",
    "caller_hash",
    "compute_hmac",
    "make_state_dirs",
    "validate",
]
