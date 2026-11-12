"""Pool usage readings for ``gptme-runloops select --lane``.

A *provider* turns a pool's options (the ``usage_*`` keys of its
``[pools.<name>]`` table) into a :class:`PoolReading`: the used fraction of
the pool's weekly window, the pace gap when known, the reading's age and where
it came from. A reading with ``used=None`` means "unknown", and the lane policy
then runs base effort and never reports ``no_capacity`` (fail open).

Providers are registered in :data:`PROVIDERS` by name and chosen per pool with
``provider = "<name>"`` (default ``claude-usage``). Add a pool type by adding a
function here; :func:`read_pool_usage` is the only entry point and never
raises.

``pace_gap`` uses the canonical sign of ``gptme_subscription``'s
``PacingSnapshot``: ``used - target``, positive means ahead of (burning faster
than) even pace.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Cache written by ``scripts/check-claude-usage.sh`` (its default location).
DEFAULT_CLAUDE_USAGE_CACHE = "/tmp/claude-usage-cache.json"
DEFAULT_MAX_AGE_S = 1800.0
DEFAULT_TIMEOUT_S = 15.0  # on the launch path; the cache read is the cheap route
WEEK_SECONDS = 7 * 24 * 3600
#: Pace target used by check-claude-usage's ``_pacing``: 90% of the week by reset.
CLAUDE_PACE_TARGET = 0.9

#: Pool keys read by providers (validated by the policy parser).
USAGE_OPTION_KEYS = frozenset(
    {"usage_command", "usage_cache", "usage_max_age_s", "usage_timeout_s"}
)


@dataclass(frozen=True)
class PoolReading:
    """One reading of a pool's weekly usage. ``used=None`` means unknown."""

    used: float | None = None
    pace_gap: float | None = None
    age_s: float | None = None
    source: str = "unknown"
    note: str | None = None

    @property
    def known(self) -> bool:
        return self.used is not None

    def to_dict(self, name: str) -> dict[str, object]:
        return {
            "name": name,
            "used": self.used,
            "pace_gap": self.pace_gap,
            "age_s": None if self.age_s is None else int(self.age_s),
            "source": self.source,
        }


UsageProvider = Callable[[Mapping[str, Any]], PoolReading]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _pace_gap(
    used: float, resets_in_seconds: float | None, pacing: object
) -> float | None:
    """Canonical pace gap for the weekly window, or None when the reset is unknown."""
    if resets_in_seconds is None or resets_in_seconds <= 0:
        return None
    try:
        from gptme_subscription.routing import compute_window_pacing_snapshot
    except Exception:  # optional package missing or broken: use the script's value
        legacy = _number(pacing.get("pace_gap")) if isinstance(pacing, dict) else None
        # scripts/check-claude-usage-parser.py computes ``target = elapsed * 0.9;
        # pace_gap = target - actual`` (negative = overusing); flip it to the
        # canonical ``used - target`` sign (positive = ahead of even pace).
        return None if legacy is None else round(-legacy, 3)
    snapshot = compute_window_pacing_snapshot(
        used,
        int(resets_in_seconds),
        WEEK_SECONDS,
        target_utilization=CLAUDE_PACE_TARGET,
    )
    return None if snapshot is None else round(snapshot.pace_gap, 3)


def parse_claude_usage(data: object) -> tuple[float | None, float | None]:
    """Return ``(weekly used, pace gap)`` from check-claude-usage JSON output."""
    if not isinstance(data, dict):
        return None, None
    seven_day = data.get("seven_day")
    if not isinstance(seven_day, dict):
        return None, None
    used = _number(seven_day.get("utilization"))
    if used is None or used < 0:
        return None, None
    resets = _number(seven_day.get("resets_in_seconds"))
    return used, _pace_gap(used, resets, data.get("_pacing"))


def _file_age(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def read_claude_usage(options: Mapping[str, Any]) -> PoolReading:
    """Claude subscription weekly usage, as read by ``check-claude-usage.sh``.

    With ``usage_command`` set, runs it (expects the script's ``--json``
    output on stdout); otherwise reads the script's cache file
    (``usage_cache``, default ``/tmp/claude-usage-cache.json``), which costs
    nothing but is only as fresh as the last caller of the script.
    """
    cache = Path(
        os.path.expanduser(str(options.get("usage_cache", DEFAULT_CLAUDE_USAGE_CACHE)))
    )
    command = options.get("usage_command")
    if command:
        argv = (
            shlex.split(command)
            if isinstance(command, str)
            else [str(a) for a in command]
        )
        argv = [os.path.expanduser(a) for a in argv]
        timeout = float(options.get("usage_timeout_s", DEFAULT_TIMEOUT_S))
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        if proc.returncode != 0:
            return PoolReading(
                source="claude-usage:command",
                note=f"usage command exited {proc.returncode}",
            )
        used, gap = parse_claude_usage(json.loads(proc.stdout))
        # The script serves its own cache when fresh; its mtime is the data's age.
        age = _file_age(cache)
        return PoolReading(
            used, gap, 0.0 if age is None else age, "claude-usage:command"
        )
    age = _file_age(cache)
    if age is None:
        return PoolReading(
            source="claude-usage:cache", note=f"no usage cache at {cache}"
        )
    used, gap = parse_claude_usage(json.loads(cache.read_text()))
    return PoolReading(used, gap, age, "claude-usage:cache")


def _read_none(options: Mapping[str, Any]) -> PoolReading:
    return PoolReading(source="none", note="pool has no usage provider")


PROVIDERS: dict[str, UsageProvider] = {
    "claude-usage": read_claude_usage,
    "none": _read_none,
}


def read_pool_usage(provider: str, options: Mapping[str, Any]) -> PoolReading:
    """Read a pool through its provider. Never raises; failures read as unknown."""
    fn = PROVIDERS.get(provider)
    if fn is None:
        return PoolReading(source=provider, note=f"unknown usage provider {provider!r}")
    try:
        reading = fn(options)
    except Exception as exc:
        return PoolReading(
            source=provider, note=f"provider error: {type(exc).__name__}: {exc}"
        )
    if reading.used is None and reading.note is None:
        reading = PoolReading(
            None, None, reading.age_s, reading.source, "usage not reported"
        )
    max_age = _number(options.get("usage_max_age_s", DEFAULT_MAX_AGE_S))
    if (
        reading.used is not None
        and reading.age_s is not None
        and max_age is not None
        and reading.age_s > max_age
    ):
        return PoolReading(
            None,
            None,
            reading.age_s,
            reading.source,
            f"stale reading: {int(reading.age_s)}s old > usage_max_age_s {int(max_age)}",
        )
    return reading
