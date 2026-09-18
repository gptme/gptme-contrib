"""Read-side registry: state-dir → allow/deny + reason.

Callers build an ordered list of :class:`BlockCheck` candidates using their own
policy (which route, which scoped key, which backend rules apply), then ask the
registry to evaluate them. The registry owns the semantics that must be
identical everywhere: a block is active while its deadline is in the future, and
a garbled file fails **open** with a recorded warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .contract import (
    ARM_BLOCK_KINDS,
    arm_block_path,
    backend_block_path,
    openrouter_block_path,
    parse_until,
    pool_block_path,
)


@dataclass(frozen=True)
class BlockCheck:
    """A candidate block file, named by the reason it would report."""

    reason: str
    path: Path


@dataclass
class BlockVerdict:
    """Outcome of evaluating an ordered list of checks.

    ``blocked`` is True at the first active (future-dated) block. Warnings
    record every fail-open encountered along the way, so a corrupt file is
    visible rather than silently ignored.
    """

    blocked: bool = False
    reason: str | None = None
    until: datetime | None = None
    file: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"blocked": self.blocked}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.until is not None:
            out["until"] = self.until.isoformat()
        if self.file is not None:
            out["file"] = self.file
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


class BlockRegistry:
    """Evaluate block files under a single state directory."""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)

    # -- check construction -------------------------------------------------

    def arm_checks(self, backend: str, model: str) -> list[BlockCheck]:
        """The three arm-level checks in evaluation order (crash, daily, quality)."""
        return [
            BlockCheck(kind, arm_block_path(self.state_dir, backend, model, kind))
            for kind in ARM_BLOCK_KINDS
        ]

    def backend_check(self, backend: str) -> BlockCheck:
        return BlockCheck(
            "backend_blocked", backend_block_path(self.state_dir, backend)
        )

    def pool_check(self, pool: str) -> BlockCheck:
        return BlockCheck("pool_blocked", pool_block_path(self.state_dir, pool))

    def openrouter_check(self, context: str | None = None) -> BlockCheck:
        return BlockCheck(
            "openrouter_daily_limit", openrouter_block_path(self.state_dir, context)
        )

    # -- evaluation ---------------------------------------------------------

    def is_blocked(
        self, check: BlockCheck, now: datetime | None = None
    ) -> tuple[bool, datetime | None, str | None]:
        """Evaluate one check.

        Returns ``(blocked, until, warning)``. Missing file → not blocked, no
        warning. Unreadable/garbled → not blocked, with a fail-open warning.
        """
        if not check.path.is_file():
            return False, None, None
        try:
            raw = check.path.read_text()
        except OSError as exc:
            return False, None, f"{check.reason}: unreadable ({exc}) — fail-open"
        until = parse_until(raw)
        if until is None:
            return (
                False,
                None,
                f"{check.reason}: garbled timestamp {raw.strip()!r} — fail-open",
            )
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if until > now:
            return True, until, None
        return False, None, None

    def check(
        self, checks: list[BlockCheck], now: datetime | None = None
    ) -> BlockVerdict:
        """Evaluate candidates in order; first active block wins."""
        if now is None:
            now = datetime.now(timezone.utc)
        verdict = BlockVerdict()
        for check in checks:
            blocked, until, warning = self.is_blocked(check, now=now)
            if warning is not None:
                verdict.warnings.append(warning)
            if blocked:
                verdict.blocked = True
                verdict.reason = check.reason
                verdict.until = until
                verdict.file = str(check.path)
                return verdict
        return verdict
