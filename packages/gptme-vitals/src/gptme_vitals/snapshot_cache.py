"""Per-signal snapshot cache for expensive vitals collectors.

A dashboard run can call dozens of collectors, many of which shell out or walk
large trees; under IO contention a full run took 180s on a loaded host. Most of
those signals move on a minutes-to-hours timescale, so an interactive call can
serve a snapshot up to ``GPTME_VITALS_SIGNAL_CACHE_TTL`` seconds old (default
15 min) without changing what any consumer sees.

Contract guarantees:

* The cached value is only ever served when it round-trips through JSON
  unchanged (``json.loads(json.dumps(value)) == value``). A collector whose
  result carries datetimes/tuples/int keys is never cached — it is simply
  recomputed — so callers can never observe a type change.
* ``ttl_seconds <= 0`` (``GPTME_VITALS_SIGNAL_CACHE_TTL=0`` or ``--fresh``)
  disables reads AND writes: the cache can be turned off with no side effects.
* ``key`` scopes a snapshot to its arguments (e.g. the ``days`` window); a key
  mismatch is a miss.
* Writes are atomic (tmp + rename) and best-effort: a read-only or missing
  cache dir degrades to "always compute".
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 900
TTL_ENV = "GPTME_VITALS_SIGNAL_CACHE_TTL"
DIR_ENV = "GPTME_VITALS_SIGNAL_CACHE_DIR"
# "1" = recompute every signal but still write the snapshots (a ``--fresh`` build).
REFRESH_ENV = "GPTME_VITALS_SIGNAL_CACHE_REFRESH"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def signal_cache_ttl_seconds(default: int = DEFAULT_TTL_SECONDS) -> int:
    """Return the configured snapshot TTL (``0`` disables the cache)."""
    raw = os.environ.get(TTL_ENV)
    if raw is None or not raw.strip():
        return max(0, default)
    try:
        return max(0, int(raw))
    except ValueError:
        return max(0, default)


def signal_cache_dir(default: Path) -> Path:
    """Return the cache directory: ``default`` unless ``DIR_ENV`` overrides it."""
    override = os.environ.get(DIR_ENV)
    if override:
        return Path(override)
    return default


def _snapshot_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / f"{_SAFE_NAME.sub('_', name)}.json"


def read_snapshot(
    cache_dir: Path,
    name: str,
    *,
    ttl_seconds: int,
    key: str = "",
    now: float | None = None,
) -> tuple[bool, Any]:
    """Return ``(hit, value)`` for a fresh, key-matching snapshot."""
    if ttl_seconds <= 0:
        return False, None
    path = _snapshot_path(cache_dir, name)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return False, None
    if (
        not isinstance(payload, dict)
        or payload.get("name") != name
        or payload.get("key", "") != key
    ):
        return False, None
    collected_at = payload.get("collected_at_epoch")
    if not isinstance(collected_at, int | float):
        return False, None
    current = time.time() if now is None else now
    if current - collected_at > ttl_seconds:
        return False, None
    if "value" not in payload:
        return False, None
    return True, payload["value"]


def write_snapshot(
    cache_dir: Path,
    name: str,
    value: Any,
    *,
    key: str = "",
    now: float | None = None,
) -> bool:
    """Persist ``value`` if it survives a JSON round-trip unchanged.

    Returns True when written. Never raises: cache failures must not fail
    the dashboard.
    """
    try:
        encoded = json.dumps(value)
        if json.loads(encoded) != value:
            return False
    except (TypeError, ValueError, RecursionError):
        return False
    current = time.time() if now is None else now
    payload = {
        "name": name,
        "key": key,
        "collected_at_epoch": current,
        "value": value,
    }
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{name}.", suffix=".tmp", dir=cache_dir
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp_name, _snapshot_path(cache_dir, name))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except (OSError, RecursionError):
        return False
    return True


def signal_cache_refresh() -> bool:
    """True when snapshots must be recomputed (reads bypassed) but still written."""
    return os.environ.get(REFRESH_ENV, "") == "1"


def cached_signal(
    name: str,
    compute: Callable[[], Any],
    *,
    cache_dir: Path,
    ttl_seconds: int | None = None,
    key: str = "",
    now: float | None = None,
    refresh: bool | None = None,
) -> Any:
    """Return a fresh snapshot of ``name`` or compute (and snapshot) it.

    ``ttl_seconds`` defaults to :func:`signal_cache_ttl_seconds`; ``refresh``
    defaults to :func:`signal_cache_refresh` and skips the read (recompute)
    while still persisting the new snapshot.
    """
    ttl = signal_cache_ttl_seconds() if ttl_seconds is None else max(0, ttl_seconds)
    do_refresh = signal_cache_refresh() if refresh is None else refresh
    if ttl > 0 and not do_refresh:
        hit, value = read_snapshot(cache_dir, name, ttl_seconds=ttl, key=key, now=now)
        if hit:
            return value
    value = compute()
    if ttl > 0 and not write_snapshot(cache_dir, name, value, key=key, now=now):
        # A successful computation supersedes the previous snapshot even when
        # its result cannot be cached. Never resurrect that older value.
        with contextlib.suppress(OSError):
            _snapshot_path(cache_dir, name).unlink()
    return value
