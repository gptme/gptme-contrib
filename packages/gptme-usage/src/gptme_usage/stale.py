"""Realized stale capacity at each quota reset.

A subscription quota that resets weekly (or on any fixed window) is wasted
capacity if the account ends the window well under its limit. This module
measures the windows that already ended, from periodic usage readings:

- each reading names an *account*, the utilization it saw and how long until
  that account's window resets;
- readings are grouped per account by their implied reset time
  (``observed_at + resets_in_seconds``), so a reset that drifts by a few
  minutes between readings is still one reset, while a reading taken after
  that reset starts the next window;
- the newest reading before a past reset gives the window's final
  utilization, and ``stale = 1 - utilization``.

A window whose newest reading is more than ``max_gap_hours`` before its reset
is reported as unobserved (``stale is None``) rather than guessed: usage can
still rise after the last reading, so ``1 - utilization`` would overstate it.

``account`` is an opaque host-supplied label. It must identify the account
whose quota resets, not the credential slot or agent that read it: if a slot
switches accounts mid-window, labelling readings by slot merges two accounts'
windows under one name.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_CLUSTER_HOURS = 3.0
DEFAULT_MAX_GAP_HOURS = 6.0


@dataclass(frozen=True)
class QuotaReading:
    """One utilization reading for one account's quota window."""

    account: str
    observed_at: datetime
    utilization: float  # 0.0..1.0 of the window's limit
    resets_in_seconds: float

    @property
    def reset_at(self) -> datetime:
        return self.observed_at + timedelta(seconds=self.resets_in_seconds)


@dataclass(frozen=True)
class ResetWindow:
    """A quota window that ended at ``reset_at``."""

    account: str
    reset_at: datetime
    final_utilization: float
    hours_before_reset: float  # age of the newest reading at reset time
    stale: float | None  # None when unobserved

    @property
    def observed(self) -> bool:
        return self.stale is not None


def windows_at_reset(
    readings: Iterable[QuotaReading],
    now: datetime,
    *,
    cluster_hours: float = DEFAULT_CLUSTER_HOURS,
    max_gap_hours: float = DEFAULT_MAX_GAP_HOURS,
) -> list[ResetWindow]:
    """Return one ResetWindow per (account, past reset), oldest first per account.

    Windows whose reset is still in the future are skipped. Readings with a
    negative ``resets_in_seconds`` are ignored.
    """
    by_account: dict[str, list[QuotaReading]] = {}
    for r in readings:
        if r.resets_in_seconds < 0:
            continue
        by_account.setdefault(r.account, []).append(r)

    out: list[ResetWindow] = []
    cluster_gap = timedelta(hours=cluster_hours)
    for account, items in sorted(by_account.items()):
        items.sort(key=lambda r: r.reset_at)
        clusters: list[list[QuotaReading]] = []
        for r in items:
            prev_reset = clusters[-1][-1].reset_at if clusters else None
            # Same window: the implied reset only drifted, and this reading was
            # taken no later than that reset (one taken after it is in a later
            # window, however short the window is). ``<=`` and not ``<``: a
            # reading observed exactly at the reset shares the window with the
            # previous one, otherwise two equal reset timestamps become two
            # windows and the same reading on the boundary is double-counted —
            # and the result would depend on input order, since the reset-time
            # sort preserves ties.
            if (
                prev_reset is not None
                and r.reset_at - prev_reset <= cluster_gap
                and r.observed_at <= prev_reset
            ):
                clusters[-1].append(r)
            else:
                clusters.append([r])
        for cluster in clusters:
            last = max(cluster, key=lambda r: r.observed_at)
            if last.reset_at > now:
                continue  # window still running
            gap_hours = (last.reset_at - last.observed_at).total_seconds() / 3600
            stale = (
                None if gap_hours > max_gap_hours else max(0.0, 1 - last.utilization)
            )
            out.append(
                ResetWindow(
                    account=account,
                    reset_at=last.reset_at,
                    final_utilization=last.utilization,
                    hours_before_reset=gap_hours,
                    stale=stale,
                )
            )
    return out


@dataclass(frozen=True)
class StaleSummary:
    windows: int
    observed: int
    at_or_under_target: int
    target: float

    @property
    def coverage(self) -> float:
        return self.observed / self.windows if self.windows else 0.0


def summarize(windows: Iterable[ResetWindow], target: float = 0.10) -> StaleSummary:
    """Count observed windows ending at or under ``target`` stale.

    Unobserved windows count toward ``windows`` but never toward the target,
    so missing readings lower coverage instead of passing silently.
    """
    ws = list(windows)
    observed = [w for w in ws if w.stale is not None]
    met = sum(1 for w in observed if w.stale is not None and w.stale <= target)
    return StaleSummary(
        windows=len(ws), observed=len(observed), at_or_under_target=met, target=target
    )
