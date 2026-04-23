"""Credential slot manager: offline safety checks for OAuth-backed slots.

A "slot" is a named credential file next to the live one, e.g.:

    ~/.claude/.credentials.json             # symlink → one of the slots below
    ~/.claude/.credentials.json.bob         # slot "bob"
    ~/.claude/.credentials.json.alice       # slot "alice"

All checks here are **offline** — they inspect the file's stored
``expiresAt`` and hash contents. Server-side token invalidation (valid
``expiresAt`` but API returns 401) is out of scope; the agent's autonomous
runner is expected to detect that via response classification.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, TypedDict

DEFAULT_LIVE_NAME = ".credentials.json"
DEFAULT_SLOT_TEMPLATE = ".credentials.json.{sub}"
DEFAULT_GRACE_SECONDS = 300


class DriftInfo(TypedDict):
    """Result of :meth:`SlotManager.detect_live_slot_drift`.

    ``drift`` is True when the live file matches no known slot — typically
    after an operator ran ``/login`` (which rewrites the live file but
    leaves the named slots untouched). In that state, an automated
    ``switch_to(matching_slot)`` would silently restore a stale token.
    """

    drift: bool
    matching_slot: str | None
    live_hash: str | None
    slot_hashes: dict[str, str]


@dataclass
class SwitchResult:
    """Outcome of a :meth:`SlotManager.switch_to` call.

    ``ok`` is True when the symlink was updated. Otherwise ``reason`` gives
    a short human-readable explanation ("deferred: autonomous sessions
    active", "refusing: slot expired 7m ago", "slot missing: …"). Callers
    choose whether to surface ``reason`` via print/log/telemetry.
    """

    ok: bool
    reason: str
    deferred_locks: list[str] = field(default_factory=list)


def _parse_oauth_expiry(payload: object) -> datetime | None:
    """Return the UTC ``expiresAt`` of a claudeAiOauth payload, or None.

    The credential file format is::

        {"claudeAiOauth": {"accessToken": ..., "expiresAt": <ms>, ...}}

    ``expiresAt`` is a Unix epoch in milliseconds. Returns None for
    missing/malformed payloads so callers can treat "unknown" distinctly
    from "expired".
    """
    if not isinstance(payload, dict):
        return None
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    expires_ms = oauth.get("expiresAt")
    if not isinstance(expires_ms, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(expires_ms) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def read_slot_expiry(path: Path) -> datetime | None:
    """Read ``expiresAt`` from an OAuth credential file.

    Returns None for missing files, unreadable files, malformed JSON, or
    payloads without a parseable ``claudeAiOauth.expiresAt`` field.
    """
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _parse_oauth_expiry(payload)


def slot_is_fresh(
    path: Path,
    *,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Check whether a slot's stored OAuth token is safe to switch into.

    Offline, cheap, no network: only validates ``expiresAt``. Catches the
    common case where a slot's token lapsed since it was last persisted.
    Does NOT catch server-side token invalidation with a still-future
    ``expiresAt`` — detect that in the agent's API response classifier.

    Returns ``(is_fresh, reason)``.
    """
    if not path.exists():
        return False, f"slot missing: {path.name}"
    expiry = read_slot_expiry(path)
    if expiry is None:
        return False, f"unreadable or no expiresAt in {path.name}"
    current = now or datetime.now(timezone.utc)
    if expiry <= current + timedelta(seconds=grace_seconds):
        age = int((current - expiry).total_seconds())
        if age >= 0:
            return False, f"expired {age // 60}m ago (at {expiry.isoformat()})"
        return False, f"expires within grace ({-age}s left, at {expiry.isoformat()})"
    return True, f"valid until {expiry.isoformat()}"


def _hash_file(path: Path) -> str | None:
    """Return sha256 of a file's contents, or None if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass
class SlotManager:
    """Manages a family of named OAuth credential slots sharing one live file.

    Workspace-specific behavior (switch logging, rate-limit files,
    usage-based strategy, rebalance state, …) stays in the caller. This
    class only cares about:

    - Where the slots live (``creds_dir``, ``slot_template``, ``live_name``)
    - Which slot is currently active (symlink target)
    - Whether a target slot is safe to switch into (``expiresAt``)
    - Whether the live file drifted away from every named slot
    - Actually flipping the symlink, with optional defer-if-busy guard

    Parameters
    ----------
    creds_dir:
        Directory holding the live file + named slots.
    subscriptions:
        Names of the slots to consider (e.g. ``["bob", "alice", "erik"]``).
    slot_template:
        ``str.format``-style template with one ``{sub}`` placeholder,
        joined to ``creds_dir``. Defaults to ``.credentials.json.{sub}``.
    live_name:
        Filename of the live symlink inside ``creds_dir``. Defaults to
        ``.credentials.json``.
    grace_seconds:
        Reject slots whose expiry is within this many seconds of ``now``.
    lock_guard:
        Optional zero-arg callable returning the names of holds that
        should defer automated switches. When non-empty and ``force`` is
        False, :meth:`switch_to` returns ``SwitchResult(ok=False,
        reason="deferred: ...", deferred_locks=...)`` without touching
        the symlink. Workspaces can point this at lock files, running
        sessions, or any custom busy-signal.
    on_switch:
        Optional ``(sub, reason) -> None`` callback invoked on successful
        symlink flips. Use it to persist a switch log without baking log
        paths into this package.
    logger:
        Optional ``(msg) -> None`` callback. Defaults to dropping the
        message silently so importers don't get unexpected stderr noise.
    """

    creds_dir: Path
    subscriptions: list[str]
    slot_template: str = DEFAULT_SLOT_TEMPLATE
    live_name: str = DEFAULT_LIVE_NAME
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    lock_guard: Callable[[], list[str]] | None = None
    on_switch: Callable[[str, str], None] | None = None
    logger: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        if "{sub}" not in self.slot_template:
            raise ValueError(
                f"slot_template must contain '{{sub}}', got: {self.slot_template!r}"
            )
        if not self.subscriptions:
            raise ValueError("subscriptions must be a non-empty list")

    # ---- Path helpers ----

    def slot_path(self, sub: str) -> Path:
        """Absolute path to the named slot file."""
        return self.creds_dir / self.slot_template.format(sub=sub)

    @property
    def live_path(self) -> Path:
        """Absolute path to the live credential file/symlink."""
        return self.creds_dir / self.live_name

    # ---- Introspection ----

    def get_active_subscription(self) -> str | None:
        """Return the slot name the live symlink points at, or None.

        Resolves the symlink and checks the target filename against the
        configured slot_template. Returns None when the live file is a
        regular file (i.e. drift — see :meth:`detect_live_slot_drift`) or
        doesn't match any known slot.
        """
        live = self.live_path
        if not live.is_symlink():
            return None
        target_name = live.resolve().name
        for sub in self.subscriptions:
            if target_name == self.slot_template.format(sub=sub):
                return sub
        return None

    def get_available_subscriptions(self) -> list[str]:
        """Return subscriptions whose slot file exists on disk.

        Does NOT check freshness — use :meth:`slot_is_fresh` for that.
        """
        return [sub for sub in self.subscriptions if self.slot_path(sub).exists()]

    # ---- Freshness ----

    def read_slot_expiry(self, sub: str) -> datetime | None:
        """Return ``expiresAt`` of a named slot, or None if unreadable."""
        return read_slot_expiry(self.slot_path(sub))

    def slot_is_fresh(
        self,
        sub: str,
        *,
        grace_seconds: int | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Offline freshness check for a named slot.

        See module-level :func:`slot_is_fresh` for semantics. Uses
        ``self.grace_seconds`` by default.
        """
        return slot_is_fresh(
            self.slot_path(sub),
            grace_seconds=grace_seconds
            if grace_seconds is not None
            else self.grace_seconds,
            now=now,
        )

    # ---- Drift detection ----

    def detect_live_slot_drift(self) -> DriftInfo | None:
        """Check whether the live file matches any known slot.

        Returns None when the live file doesn't exist (nothing to
        compare). Otherwise returns a :class:`DriftInfo` with
        ``drift=True`` when no slot hashes match, indicating that the live
        file was written by something other than :meth:`switch_to` — most
        commonly an operator running ``/login``.
        """
        live = self.live_path
        if not live.exists():
            return None
        live_hash = _hash_file(live)
        if live_hash is None:
            return None
        slot_hashes: dict[str, str] = {}
        matching: str | None = None
        for sub in self.subscriptions:
            slot = self.slot_path(sub)
            if not slot.exists():
                continue
            h = _hash_file(slot)
            if h is None:
                continue
            slot_hashes[sub] = h
            if h == live_hash and matching is None:
                matching = sub
        return DriftInfo(
            drift=matching is None,
            matching_slot=matching,
            live_hash=live_hash,
            slot_hashes=slot_hashes,
        )

    # ---- Switching ----

    def switch_to(
        self,
        sub: str,
        reason: str,
        *,
        force: bool = False,
    ) -> SwitchResult:
        """Flip the live symlink to point at ``sub``'s slot file.

        Safety guarantees:

        - If ``force=False`` and ``lock_guard`` returns a non-empty list,
          the switch is deferred and the symlink is not touched.
        - If the target slot is expired or unreadable, the switch is
          refused **regardless of** ``force`` — landing on a known-bad
          credential just moves the 401 crash loop to the next run.
        - On success, replaces the existing symlink atomically
          (``unlink`` then ``symlink_to``) and calls ``on_switch``.

        Returns a :class:`SwitchResult`. Callers decide whether to surface
        ``reason`` via print / logging / telemetry.
        """
        if not force and self.lock_guard is not None:
            active_locks = list(self.lock_guard())
            if active_locks:
                lock_desc = ", ".join(active_locks)
                msg = (
                    f"deferred: active lock(s) {lock_desc}; "
                    f"would switch to {sub} ({reason})"
                )
                self._log(msg)
                return SwitchResult(
                    ok=False,
                    reason=msg,
                    deferred_locks=active_locks,
                )

        slot = self.slot_path(sub)
        if not slot.exists():
            msg = f"slot missing: {slot}"
            self._log(msg)
            return SwitchResult(ok=False, reason=msg)

        fresh, fresh_reason = self.slot_is_fresh(sub)
        if not fresh:
            msg = f"refusing to switch to {sub}: {fresh_reason}"
            self._log(msg)
            return SwitchResult(ok=False, reason=msg)

        live = self.live_path
        live.unlink(missing_ok=True)
        live.symlink_to(self.slot_template.format(sub=sub))

        if self.on_switch is not None:
            self.on_switch(sub, reason)
        return SwitchResult(ok=True, reason=f"switched to {sub}")

    # ---- Internals ----

    def _log(self, msg: str) -> None:
        if self.logger is not None:
            self.logger(msg)
