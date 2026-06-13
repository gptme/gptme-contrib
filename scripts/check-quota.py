#!/usr/bin/env -S uv run python3
"""Unified quota checker for all autonomous backends.

Checks available quota across all configured backends and returns a unified
JSON report. Used by select-harness.py and autonomous-loop.sh for cross-harness
scheduling decisions.

Quota sources:
  - claude-code: Wraps check-claude-usage.sh (CC TUI, cached 10 min)
  - gptme: Heuristic based on recent session success/failure
  - Others: Assumed available (no quota API)
  - xAI: Chat completion probe (no balance API, 429 = credit exhaustion)

Usage:
    python3 scripts/check-quota.py              # JSON to stdout
    python3 scripts/check-quota.py --pretty     # Human-readable
    python3 scripts/check-quota.py --backend claude-code  # Single backend
    python3 scripts/check-quota.py --xai        # xAI credit probe only (voice provider)

Related: knowledge/technical-designs/cross-harness-orchestration.md
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_usage_pkg_src = str(REPO_ROOT / "packages" / "gptme-usage" / "src")
if _usage_pkg_src not in sys.path:
    sys.path.insert(0, _usage_pkg_src)
_subscription_pkg_src = str(REPO_ROOT / "packages" / "gptme-subscription" / "src")
if _subscription_pkg_src not in sys.path:
    sys.path.insert(0, _subscription_pkg_src)
_credential_slots_src = str(REPO_ROOT / "packages" / "credential-slots" / "src")
if _credential_slots_src not in sys.path:
    sys.path.insert(0, _credential_slots_src)
# harness_models is the shared model catalog (prices/routes/quota-sources +
# generic cost logic), canonical in the gptme-usage package (usage/capacity is a
# separate concern from subscription/slot rotation — see the gptme-usage README).
# Per-agent availability is configuration (see QUOTA_BACKENDS).
from gptme_subscription.routing import compute_window_pacing  # noqa: E402
from gptme_usage.harness_models import (  # noqa: E402
    CLAUDE_AGENT_SDK_ASSUMED_MONTHLY_CREDIT_USD,
    CLAUDE_AGENT_SDK_CREDIT_CHANGE_DATE,
    estimate_session_cost,
    estimate_tokens_from_duration,
    gptme_openrouter_context,
    is_post_agent_sdk_credit_change,
    load_quota_config,
    local_models,
    openrouter_models,
)

# Per-agent quota config (prices, TPS, model routes, quota sources) from
# ~/.config/gptme/harness-quota.toml. Loaded once and threaded through every
# harness_models call so THIS agent's config drives cost/availability — the
# shared package ships no agent's data. Empty config (no file) degrades
# gracefully to "no models configured".
QUOTA_CONFIG = load_quota_config()

SESSION_RECORDS_FILE = REPO_ROOT / "state" / "sessions" / "session-records.jsonl"
CLAUDE_CODE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_USAGE_SCRIPT = REPO_ROOT / "scripts" / "check-claude-usage.sh"
CLAUDE_USAGE_CACHE_FILE = Path("/tmp/claude-usage-cache.json")
CODEX_USAGE_CACHE_FILE = Path("/tmp/codex-usage-cache.json")
# Codex usage cache older than this is treated as stale (selector falls back to
# the crash-history heuristic). The scraper refreshes it on a 3-min TTL via
# subscription-check.sh; 15 min tolerates a couple of missed refresh cycles.
CODEX_USAGE_MAX_AGE_SECONDS = 900
OPENROUTER_USAGE_SCRIPT = REPO_ROOT / "scripts" / "check-openrouter-usage.sh"
BACKEND_QUOTA_DIR = REPO_ROOT / "state" / "backend-quota"
CLAUDE_USAGE_REFRESH_REQUEST_FILE = (
    BACKEND_QUOTA_DIR / "claude-usage-refresh-requested.txt"
)
GROK_BUILD_AUTH_FILE = Path.home() / ".grok" / "auth.json"

CLAUDE_USAGE_TIMEOUT_SECONDS = 60
FAST_CLAUDE_USAGE_TIMEOUT_SECONDS = 8
MANAGE_SUBSCRIPTION_TIMEOUT_SECONDS = 10
FAST_MANAGE_SUBSCRIPTION_TIMEOUT_SECONDS = 3
OPENROUTER_USAGE_TIMEOUT_SECONDS = 15
FAST_OPENROUTER_USAGE_TIMEOUT_SECONDS = 5
COPILOT_USAGE_TIMEOUT_SECONDS = 15
FAST_COPILOT_USAGE_TIMEOUT_SECONDS = 5
LOCAL_MODEL_TIMEOUT_SECONDS = 5
FAST_LOCAL_MODEL_TIMEOUT_SECONDS = 2
CLAUDE_USAGE_BACKGROUND_REFRESH_THROTTLE_SECONDS = 15 * 60
CLAUDE_USAGE_REFRESH_STALL_GRACE_SECONDS = CLAUDE_USAGE_TIMEOUT_SECONDS + 30
CLAUDE_USAGE_ACTIVE_REFRESH_MAX_AGE_SECONDS = CLAUDE_USAGE_TIMEOUT_SECONDS * 3
CLAUDE_USAGE_TMUX_SESSION_PREFIX = "claude-usage-check-"
_INFRA_FAILURE_DURATION_THRESHOLD_S = 120

# How many recent sessions to check for heuristic backends
HEURISTIC_LOOKBACK_HOURS = 6
# If the last session on a backend failed, how long to wait before retrying (seconds)
HEURISTIC_COOLDOWN_AFTER_FAILURE = 1800  # 30 min
SUBPROCESS_PYTHONWARNINGS_FILTER = "ignore:::requests"
CLAUDE_AGENT_SDK_CREDIT_ALERT_UTILIZATION = 0.80


@dataclass
class BackendQuota:
    """Quota status for a single backend+model combination."""

    backend: str  # e.g. "claude-code", "gptme", "codex"
    model: str  # default model for this backend
    available: bool = True  # Can we run a session right now?
    utilization: float = 0.0  # 0.0-1.0, how much quota is used
    resets_in_seconds: int = 0  # When does quota reset? (0 = unknown)
    source: str = "heuristic"  # "api", "cached", "heuristic", "assumed"
    reason: str = ""  # Why unavailable, if not available
    models_available: list[str] = field(default_factory=list)
    metadata: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    headroom: float | None = None  # 1 - utilization, when quota-backed
    pacing_target: float | None = None  # Expected utilization at this point in window
    pacing_gap: float | None = None  # actual - target (positive = ahead/overusing)
    pacing_status: str = (
        "unknown"  # underusing | on_track | overusing | blocked | unknown
    )


@dataclass
class QuotaReport:
    """Unified quota report across all backends."""

    backends: list[BackendQuota]
    timestamp: str = ""
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _window_pacing(
    utilization: float, resets_in_seconds: int, window_seconds: int
) -> tuple[float, float, str]:
    """Return pacing target/gap/status using the shared quota pacing helper."""
    result = compute_window_pacing(utilization, resets_in_seconds, window_seconds)
    if result is None:
        return 0.0, utilization, "unknown"
    elapsed_frac, gap, status = result
    return elapsed_frac, gap, status


def _combine_pacing(
    windows: list[tuple[float, int, int]],
) -> tuple[float | None, float | None, str]:
    """Combine multiple window pacing checks by the most over-budget constraint."""
    best: tuple[float, float, str] | None = None
    for util, resets_in, window_secs in windows:
        result = compute_window_pacing(util, resets_in, window_secs)
        if result is None:
            continue
        elapsed_frac, gap, status = result
        if best is None or gap > best[1]:
            best = (elapsed_frac, gap, status)
    if best is None:
        return None, None, "unknown"
    return best[0], best[1], best[2]


def _subprocess_env() -> dict[str, str]:
    """Return child env with known-noisy Python warnings suppressed."""
    env = os.environ.copy()
    existing = env.get("PYTHONWARNINGS", "").strip()
    filters = [value for value in (existing, SUBPROCESS_PYTHONWARNINGS_FILTER) if value]
    env["PYTHONWARNINGS"] = ",".join(filters)
    return env


def _sanitize_first_stderr_line(raw_stderr: str, fallback: str) -> str:
    """Return a compact one-line reason from subprocess stderr."""
    raw_err = raw_stderr.strip()
    if not raw_err:
        return fallback
    first_line = raw_err.splitlines()[0]
    return (first_line[:200] + "...") if len(first_line) > 200 else first_line


def _probe_timeout(normal: int, fast: int, *, fast_mode: bool) -> int:
    """Return the timeout budget for a live quota probe."""
    return fast if fast_mode else normal


def _claude_usage_cache_ttl_seconds() -> int:
    """Return the configured Claude usage cache TTL."""
    raw = os.environ.get("CLAUDE_USAGE_CACHE_TTL", "600")
    try:
        return max(0, int(raw))
    except ValueError:
        return 600


def _file_age_seconds(path: Path) -> int | None:
    """Return file age in seconds, or None when unavailable."""
    try:
        return max(
            0,
            int(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime),
        )
    except OSError:
        return None


def _file_mtime_seconds(path: Path) -> float | None:
    """Return file mtime in epoch seconds, or None when unavailable."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _load_cached_claude_usage() -> tuple[dict[str, object], int] | None:
    """Return the last Claude usage snapshot plus its age in seconds."""
    try:
        data = json.loads(CLAUDE_USAGE_CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    age_seconds = _file_age_seconds(CLAUDE_USAGE_CACHE_FILE)
    if age_seconds is None:
        return None
    return data, age_seconds


def _load_codex_main_pacing() -> tuple[float, int, float, int] | None:
    """Read the codex usage cache main tier.

    Returns (5h_util, 5h_reset_s, weekly_util, weekly_reset_s) from the scraped
    /status snapshot, or None when the cache is missing, malformed, or stale
    (>CODEX_USAGE_MAX_AGE_SECONDS). The scraper (check-codex-usage.sh) already
    normalizes codex's "% left" to utilization (1 - left/100). gpt-5.4 and
    gpt-5.5 share this main (non-Spark) Codex pool, so the same numbers apply to
    both models.
    """
    age = _file_age_seconds(CODEX_USAGE_CACHE_FILE)
    if age is None or age > CODEX_USAGE_MAX_AGE_SECONDS:
        return None
    try:
        data = json.loads(CODEX_USAGE_CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    main = data.get("main") if isinstance(data, dict) else None
    if not isinstance(main, dict):
        return None
    five_hour = main.get("five_hour")
    weekly = main.get("weekly")
    if not isinstance(five_hour, dict) or not isinstance(weekly, dict):
        return None
    try:
        return (
            float(five_hour["utilization"]),
            int(five_hour["resets_in_seconds"]),
            float(weekly["utilization"]),
            int(weekly["resets_in_seconds"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _shared_codex_pool_quota(backend: str, model: str) -> BackendQuota | None:
    """Build a pacing-aware quota row from the scraped codex /status usage cache,
    or None when the cache is stale/missing (caller decides the fallback).

    The ChatGPT Codex subscription pool (5h + weekly) is shared by BOTH:
      - codex CLI models `codex:gpt-5.4` / `codex:gpt-5.5`, and
      - gptme's openai-subscription models `gptme:gpt-5.4` / `gptme:gpt-5.5`
        (same account, reached via the openai-subscription endpoint).
    So a single scraped /status snapshot governs all four rows. Mirrors
    claude-code pacing-aware selection: combines 5h + weekly pacing, marks the
    row unavailable when utilization is effectively exhausted (>=0.98), and
    otherwise exposes utilization/headroom/pacing so the selector proactively
    deprioritizes a near-exhausted pool instead of only discovering exhaustion
    reactively via crash-loop / 429 clusters.
    """
    pacing = _load_codex_main_pacing()
    if pacing is None:
        return None
    fh_util, fh_reset, wk_util, wk_reset = pacing
    target, gap, pacing_status = _combine_pacing(
        [
            (fh_util, fh_reset, 18000),
            (wk_util, wk_reset, 604800),
        ]
    )
    util = max(fh_util, wk_util)
    available = util < 0.98
    if fh_util >= wk_util:
        reset = fh_reset
        reason = f"5h: {fh_util:.0%}, 7d: {wk_util:.0%}"
    else:
        reset = wk_reset
        reason = f"7d: {wk_util:.0%}, 5h: {fh_util:.0%}"
    if not available:
        reason = f"BLOCKED — {reason}"
    return BackendQuota(
        backend=backend,
        model=model,
        available=available,
        utilization=util,
        resets_in_seconds=reset,
        source="codex-usage-cache",
        reason=reason,
        models_available=[model],
        headroom=1.0 - util,
        pacing_target=target,
        pacing_gap=gap,
        pacing_status=("blocked" if not available else pacing_status),
    )


def _build_codex_quota_from_usage(model: str) -> BackendQuota:
    """codex:* row: scraped /status pacing when fresh, else crash heuristic."""
    return _shared_codex_pool_quota("codex", model) or check_heuristic_quota(
        "codex", model, [model]
    )


def _pending_claude_usage_refresh_metadata() -> dict[str, int]:
    """Return metadata for an in-flight Claude usage refresh request.

    The fast-path quota check can legitimately return cached data, but when a
    refresh request is newer than the cache itself we should surface whether the
    cache is merely pending an update or appears stalled.
    """
    request_age_seconds = _file_age_seconds(CLAUDE_USAGE_REFRESH_REQUEST_FILE)
    if request_age_seconds is None:
        return {}

    request_mtime = _file_mtime_seconds(CLAUDE_USAGE_REFRESH_REQUEST_FILE)
    cache_mtime = _file_mtime_seconds(CLAUDE_USAGE_CACHE_FILE)
    if request_mtime is None or cache_mtime is None or request_mtime <= cache_mtime:
        return {}

    metadata: dict[str, int] = {
        "refresh_request_age_seconds": request_age_seconds,
    }
    if request_age_seconds >= CLAUDE_USAGE_REFRESH_STALL_GRACE_SECONDS:
        metadata["refresh_stalled_seconds"] = request_age_seconds
    return metadata


def _claude_usage_refresh_in_progress() -> bool:
    """Return True when a recent tmux-backed Claude usage refresh is live."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}|#{session_created}"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_subprocess_env(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False

    if result.returncode != 0:
        return False

    now = int(datetime.now(timezone.utc).timestamp())
    for raw_line in result.stdout.splitlines():
        if "|" not in raw_line:
            continue
        session_name, created_raw = raw_line.split("|", 1)
        if not session_name.startswith(CLAUDE_USAGE_TMUX_SESSION_PREFIX):
            continue
        try:
            created_at = int(created_raw)
        except ValueError:
            continue
        age_seconds = max(0, now - created_at)
        if age_seconds <= CLAUDE_USAGE_ACTIVE_REFRESH_MAX_AGE_SECONDS:
            return True

    return False


def _quota_section(data: dict[str, object], key: str) -> dict[str, object]:
    """Return a named quota subsection or an empty mapping."""
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _quota_float(section: dict[str, object], key: str) -> float:
    """Return a numeric quota field as float."""
    value = section.get(key)
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _quota_int(section: dict[str, object], key: str) -> int:
    """Return a numeric quota field as int."""
    value = section.get(key)
    if isinstance(value, int | float):
        return int(value)
    return 0


def _parse_record_timestamp(value: object) -> datetime | None:
    """Parse a session-record timestamp as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_claude_code_timestamp(value: object) -> datetime | None:
    """Parse a Claude Code JSONL timestamp as UTC."""
    return _parse_record_timestamp(value)


def _next_month_start(dt: datetime) -> datetime:
    """Return the UTC start of the month after ``dt``."""
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)


def _claude_agent_sdk_credit_window(
    now: datetime,
) -> tuple[datetime, datetime, int]:
    """Return the Agent SDK credit accounting window.

    The first post-change window starts on June 15, 2026, not June 1, because
    Agent SDK usage before that date still belongs to the old Claude Max limits.
    Later windows reset at the UTC start of each calendar month.
    """
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    start = max(month_start, CLAUDE_AGENT_SDK_CREDIT_CHANGE_DATE)
    end = _next_month_start(now)
    window_seconds = max(1, int((end - start).total_seconds()))
    return start, end, window_seconds


def _is_unmeasurable_infra_failure(rec: dict[str, object]) -> bool:
    """Return True for short zero-token sessions that never reached the model."""
    in_tok = rec.get("input_tokens")
    out_tok = rec.get("output_tokens")
    tok_total = rec.get("token_count")
    has_numeric_zero_triplet = all(
        isinstance(value, int | float) and not isinstance(value, bool)
        for value in (in_tok, out_tok, tok_total)
    )

    if has_numeric_zero_triplet:
        assert isinstance(in_tok, int | float)
        assert isinstance(out_tok, int | float)
        assert isinstance(tok_total, int | float)
        if int(in_tok) == 0 and int(out_tok) == 0 and int(tok_total) == 0:
            return True

    duration = rec.get("duration_seconds")
    return (
        tok_total is None
        and rec.get("outcome") != "productive"
        and isinstance(duration, int | float)
        and duration < _INFRA_FAILURE_DURATION_THRESHOLD_S
    )


def _estimate_claude_agent_sdk_credit_usage_from_records(
    *,
    start: datetime,
    end: datetime,
    records_file: Path,
) -> dict[str, object]:
    """Estimate Agent SDK credit consumption from canonical session records."""
    used_usd = 0.0
    sessions = 0
    uncosted_sessions = 0
    estimated_sessions = 0
    skipped_unmeasurable_sessions = 0
    if records_file.exists():
        with records_file.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("harness") != "claude-code":
                    continue
                ts = _parse_record_timestamp(rec.get("timestamp"))
                if ts is None or ts < start or ts >= end:
                    continue
                if _is_unmeasurable_infra_failure(rec):
                    skipped_unmeasurable_sessions += 1
                    continue
                sessions += 1
                cost = estimate_session_cost(
                    str(rec.get("harness") or ""),
                    str(rec.get("model") or ""),
                    input_tokens=rec.get("input_tokens"),
                    output_tokens=rec.get("output_tokens"),
                    cache_creation_tokens=rec.get("cache_creation_tokens"),
                    cache_read_tokens=rec.get("cache_read_tokens"),
                    token_count=rec.get("token_count"),
                    config=QUOTA_CONFIG,
                )
                estimated = False
                if cost is None:
                    duration = rec.get("duration_seconds")
                    if isinstance(duration, int | float) and duration > 0:
                        est_tokens = estimate_tokens_from_duration(
                            str(rec.get("harness") or ""),
                            str(rec.get("model") or ""),
                            int(duration),
                            config=QUOTA_CONFIG,
                        )
                        if est_tokens is not None:
                            cost = estimate_session_cost(
                                str(rec.get("harness") or ""),
                                str(rec.get("model") or ""),
                                token_count=est_tokens,
                                config=QUOTA_CONFIG,
                            )
                            estimated = cost is not None
                if cost is None:
                    uncosted_sessions += 1
                    continue
                if estimated:
                    estimated_sessions += 1
                used_usd += cost

    return {
        "used_usd": round(used_usd, 6),
        "sessions": sessions,
        "uncosted_sessions": uncosted_sessions,
        "estimated_sessions": estimated_sessions,
        "skipped_unmeasurable_sessions": skipped_unmeasurable_sessions,
        "usage_turns": None,
        "source": "session_records",
    }


def _estimate_claude_agent_sdk_credit_usage_from_logs(
    *,
    start: datetime,
    end: datetime,
    claude_projects_dir: Path,
) -> dict[str, object] | None:
    """Estimate Agent SDK credit consumption from raw Claude Code JSONL logs."""
    if not claude_projects_dir.exists():
        return None

    used_usd = 0.0
    sessions = 0
    uncosted_sessions = 0
    usage_turns = 0

    for log_file in claude_projects_dir.glob("*/*.jsonl"):
        try:
            fallback_ts = datetime.fromtimestamp(log_file.stat().st_mtime, timezone.utc)
        except OSError:
            continue

        session_turns = 0
        session_costed = False
        session_uncosted = False

        try:
            with log_file.open() as f:
                for line in f:
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") != "assistant":
                        continue
                    inner = msg.get("message")
                    if not isinstance(inner, dict):
                        continue
                    usage = inner.get("usage")
                    if not isinstance(usage, dict) or not usage:
                        continue

                    ts = _parse_claude_code_timestamp(msg.get("timestamp"))
                    if ts is None:
                        ts = fallback_ts
                    if ts < start or ts >= end:
                        continue

                    session_turns += 1
                    usage_turns += 1
                    cost = estimate_session_cost(
                        "claude-code",
                        str(inner.get("model") or ""),
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        cache_creation_tokens=usage.get("cache_creation_input_tokens"),
                        cache_read_tokens=usage.get("cache_read_input_tokens"),
                        config=QUOTA_CONFIG,
                    )
                    if cost is None:
                        session_uncosted = True
                        continue
                    session_costed = True
                    used_usd += cost
        except OSError:
            continue

        if session_turns:
            sessions += 1
            if session_uncosted and not session_costed:
                uncosted_sessions += 1

    if sessions == 0:
        return None

    return {
        "used_usd": round(used_usd, 6),
        "sessions": sessions,
        "uncosted_sessions": uncosted_sessions,
        "estimated_sessions": 0,
        "skipped_unmeasurable_sessions": 0,
        "usage_turns": usage_turns,
        "source": "claude_code_logs",
    }


def _estimate_claude_agent_sdk_credit_usage(
    *,
    now: datetime | None = None,
    records_file: Path | None = None,
    claude_projects_dir: Path | None = None,
) -> dict[str, object]:
    """Estimate current-month Agent SDK credit consumption."""
    now = now or datetime.now(timezone.utc)
    records_file_supplied = records_file is not None
    claude_projects_dir_supplied = claude_projects_dir is not None
    records_file = records_file or SESSION_RECORDS_FILE
    claude_projects_dir = claude_projects_dir or CLAUDE_CODE_PROJECTS_DIR
    start, end, window_seconds = _claude_agent_sdk_credit_window(now)

    usage = None
    if claude_projects_dir_supplied or not records_file_supplied:
        usage = _estimate_claude_agent_sdk_credit_usage_from_logs(
            start=start,
            end=end,
            claude_projects_dir=claude_projects_dir,
        )
    if usage is None:
        usage = _estimate_claude_agent_sdk_credit_usage_from_records(
            start=start,
            end=end,
            records_file=records_file,
        )

    used_value = usage.get("used_usd", 0.0)
    used_usd = float(used_value) if isinstance(used_value, int | float | str) else 0.0
    utilization = used_usd / CLAUDE_AGENT_SDK_ASSUMED_MONTHLY_CREDIT_USD
    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_seconds": window_seconds,
        "resets_in_seconds": max(0, int((end - now).total_seconds())),
        "monthly_credit_usd": CLAUDE_AGENT_SDK_ASSUMED_MONTHLY_CREDIT_USD,
        "used_usd": round(used_usd, 6),
        "remaining_usd": round(
            max(0.0, CLAUDE_AGENT_SDK_ASSUMED_MONTHLY_CREDIT_USD - used_usd),
            6,
        ),
        "utilization": utilization,
        "sessions": usage["sessions"],
        "uncosted_sessions": usage["uncosted_sessions"],
        "estimated_sessions": usage["estimated_sessions"],
        "skipped_unmeasurable_sessions": usage["skipped_unmeasurable_sessions"],
        "usage_turns": usage["usage_turns"],
        "source": usage["source"],
    }


def _apply_claude_agent_sdk_credit_budget(
    results: list[BackendQuota],
    *,
    now: datetime | None = None,
) -> list[BackendQuota]:
    """Replace old Claude Max quota semantics with the post-June-15 credit pool."""
    now = now or datetime.now(timezone.utc)
    if not is_post_agent_sdk_credit_change(now):
        return results

    usage = _estimate_claude_agent_sdk_credit_usage(now=now)

    def _usage_float(key: str) -> float:
        value = usage.get(key)
        return float(value) if isinstance(value, int | float | str) else 0.0

    def _usage_int(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, int | float | str) else 0

    utilization_raw = _usage_float("utilization")
    utilization = min(1.0, utilization_raw)
    resets_in = _usage_int("resets_in_seconds")
    window_seconds = _usage_int("window_seconds")
    target, gap, pacing_status = _window_pacing(
        utilization_raw, resets_in, window_seconds
    )
    credit = _usage_float("monthly_credit_usd")
    used = _usage_float("used_usd")
    blocked = utilization_raw >= 1.0

    if blocked:
        detail = (
            f"BLOCKED — Agent SDK credit ${used:.2f}/${credit:.0f} "
            f"({utilization_raw:.0%})"
        )
        pacing_status = "blocked"
    elif utilization_raw >= CLAUDE_AGENT_SDK_CREDIT_ALERT_UTILIZATION:
        detail = (
            f"WARN — Agent SDK credit ${used:.2f}/${credit:.0f} ({utilization_raw:.0%})"
        )
    else:
        detail = f"Agent SDK credit ${used:.2f}/${credit:.0f} ({utilization_raw:.0%})"

    for entry in results:
        if entry.backend != "claude-code":
            continue
        # Post-June-15: the credit pool is the sole availability gate for Agent
        # SDK calls. Old 5h/7d limits apply only to interactive (non-agent)
        # sessions and must not block agent dispatches when credit has headroom.
        # Overwriting (not AND-ing) entry.available avoids a stale 5h/7d
        # available=False forcing a ~30-day credit-window wait on the scheduler.
        entry.available = not blocked
        entry.utilization = utilization
        entry.resets_in_seconds = resets_in
        entry.reason = detail
        entry.headroom = 1.0 - utilization
        entry.pacing_target = target
        entry.pacing_gap = gap
        entry.pacing_status = pacing_status
        entry.metadata.update(
            {
                "agent_sdk_credit_mode": True,
                "monthly_credit_usd": credit,
                "monthly_credit_used_usd": used,
                "monthly_credit_remaining_usd": _usage_float("remaining_usd"),
                "monthly_credit_utilization": round(utilization_raw, 6),
                "monthly_credit_sessions": _usage_int("sessions"),
                "monthly_credit_uncosted_sessions": _usage_int("uncosted_sessions"),
                "monthly_credit_estimated_sessions": _usage_int("estimated_sessions"),
                "monthly_credit_usage_turns": _usage_int("usage_turns"),
                "monthly_credit_usage_source": str(usage["source"]),
                "monthly_credit_skipped_unmeasurable_sessions": _usage_int(
                    "skipped_unmeasurable_sessions"
                ),
                "monthly_credit_window_start": str(usage["window_start"]),
                "monthly_credit_window_end": str(usage["window_end"]),
            }
        )

    return results


def _build_claude_code_results(
    data: dict[str, object],
    *,
    source: str,
    cache_age_seconds: int | None = None,
) -> list[BackendQuota]:
    """Build Claude Code quota rows from a parsed usage snapshot."""
    results: list[BackendQuota] = []
    cache_metadata = (
        {"cache_age_seconds": cache_age_seconds}
        if cache_age_seconds is not None
        else {}
    )

    # Parse five_hour (session) and seven_day (weekly) quota
    five_hour = _quota_section(data, "five_hour")
    seven_day = _quota_section(data, "seven_day")

    five_hour_util = _quota_float(five_hour, "utilization")
    seven_day_util = _quota_float(seven_day, "utilization")
    five_hour_reset = _quota_int(five_hour, "resets_in_seconds")
    seven_day_reset = _quota_int(seven_day, "resets_in_seconds")
    opus_target, opus_gap, opus_pacing_status = _combine_pacing(
        [
            (five_hour_util, five_hour_reset, 18000),
            (seven_day_util, seven_day_reset, 604800),
        ]
    )

    # API usage near 100% is effectively exhausted; otherwise let pacing-aware
    # selection handle whether Claude Code is ahead or behind the ideal curve.
    opus_util = max(five_hour_util, seven_day_util)
    opus_available = opus_util < 0.98
    if five_hour_util >= seven_day_util:
        opus_reset = five_hour_reset
        opus_reason = f"5h: {five_hour_util:.0%}, 7d: {seven_day_util:.0%}"
    else:
        opus_reset = seven_day_reset
        opus_reason = f"7d: {seven_day_util:.0%}, 5h: {five_hour_util:.0%}"
    if not opus_available:
        opus_reason = f"BLOCKED — {opus_reason}"

    results.append(
        BackendQuota(
            backend="claude-code",
            model="opus",
            available=opus_available,
            utilization=opus_util,
            resets_in_seconds=opus_reset,
            source=source,
            reason=opus_reason,
            models_available=["opus"],
            metadata=dict(cache_metadata),
            headroom=1.0 - opus_util,
            pacing_target=opus_target,
            pacing_gap=opus_gap,
            pacing_status=("blocked" if not opus_available else opus_pacing_status),
        )
    )

    # Sonnet: uses separate weekly quota, but same session quota
    sonnet_weekly = _quota_section(data, "seven_day_sonnet")
    sonnet_weekly_util = _quota_float(sonnet_weekly, "utilization")
    sonnet_weekly_reset = _quota_int(sonnet_weekly, "resets_in_seconds")
    # The usage parser carries forward the last good Sonnet-weekly value from the
    # fallback cache (tagged `_stale`) when live /usage fails to paint that bar.
    # Surface that so switching/availability decisions know the figure is stale.
    sonnet_weekly_stale = bool(sonnet_weekly.get("_stale"))
    sonnet_weekly_stale_source = sonnet_weekly.get("_stale_source")
    sonnet_target, sonnet_gap, sonnet_pacing_status = _combine_pacing(
        [
            (five_hour_util, five_hour_reset, 18000),
            (sonnet_weekly_util, sonnet_weekly_reset, 604800),
        ]
    )
    sonnet_util = max(five_hour_util, sonnet_weekly_util)
    sonnet_available = sonnet_util < 0.98
    if five_hour_util >= sonnet_weekly_util:
        sonnet_reset = five_hour_reset
        sonnet_reason = f"5h: {five_hour_util:.0%}, 7d-sonnet: {sonnet_weekly_util:.0%}"
    else:
        sonnet_reset = sonnet_weekly_reset
        sonnet_reason = f"7d-sonnet: {sonnet_weekly_util:.0%}, 5h: {five_hour_util:.0%}"
    if not sonnet_available:
        sonnet_reason = f"BLOCKED — {sonnet_reason}"
    if sonnet_weekly_stale:
        sonnet_reason = f"{sonnet_reason} (7d-sonnet STALE: carried forward)"

    sonnet_metadata: dict[str, str | int | float | bool | None] = dict(cache_metadata)
    if sonnet_weekly_stale:
        sonnet_metadata["sonnet_weekly_stale"] = True
        if isinstance(sonnet_weekly_stale_source, str):
            sonnet_metadata["sonnet_weekly_stale_source"] = sonnet_weekly_stale_source

    results.append(
        BackendQuota(
            backend="claude-code",
            model="sonnet",
            available=sonnet_available,
            utilization=sonnet_util,
            resets_in_seconds=sonnet_reset,
            source=source,
            reason=sonnet_reason,
            models_available=["sonnet"],
            metadata=sonnet_metadata,
            headroom=1.0 - sonnet_util,
            pacing_target=sonnet_target,
            pacing_gap=sonnet_gap,
            pacing_status=("blocked" if not sonnet_available else sonnet_pacing_status),
        )
    )

    return results


def _augment_claude_results_with_subscription_state(
    results: list[BackendQuota], *, fast: bool
) -> list[BackendQuota]:
    """Add subscription-routing metadata to Claude Code quota rows."""
    try:
        sub_script = REPO_ROOT / "scripts" / "manage-subscription.py"
        if sub_script.exists() and not is_post_agent_sdk_credit_change():
            sub_proc = subprocess.run(
                [sys.executable, str(sub_script), "--json"],
                capture_output=True,
                env=_subprocess_env(),
                text=True,
                timeout=_probe_timeout(
                    MANAGE_SUBSCRIPTION_TIMEOUT_SECONDS,
                    FAST_MANAGE_SUBSCRIPTION_TIMEOUT_SECONDS,
                    fast_mode=fast,
                ),
            )
            if sub_proc.returncode == 0:
                sub_data = json.loads(sub_proc.stdout)
                if sub_data.get("action") == "switch" and sub_data.get("target"):
                    best_sub = sub_data["target"]
                    for entry in results:
                        entry.metadata["subscription_switch_pending"] = True
                        entry.metadata["best_sub"] = best_sub
                        # effective_utilization=0.0 signals that a fresh sub is
                        # available; select-harness.py uses this instead of the
                        # current (high) utilization for quota pressure scoring.
                        entry.metadata["effective_utilization"] = 0.0
    except Exception:
        pass  # Non-fatal — quota data is still valid without subscription info

    return _apply_claude_agent_sdk_credit_budget(results)


def _request_claude_usage_refresh_in_background() -> bool:
    """Kick off a throttled background Claude usage refresh."""
    if not CLAUDE_USAGE_SCRIPT.exists():
        return False

    if _claude_usage_refresh_in_progress():
        return False

    try:
        BACKEND_QUOTA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    request_age = _file_age_seconds(CLAUDE_USAGE_REFRESH_REQUEST_FILE)
    if (
        request_age is not None
        and request_age < CLAUDE_USAGE_BACKGROUND_REFRESH_THROTTLE_SECONDS
    ):
        return False

    try:
        fd = os.open(
            CLAUDE_USAGE_REFRESH_REQUEST_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
    except FileExistsError:
        if request_age is None:
            return False
        try:
            CLAUDE_USAGE_REFRESH_REQUEST_FILE.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        try:
            fd = os.open(
                CLAUDE_USAGE_REFRESH_REQUEST_FILE,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except (FileExistsError, OSError):
            return False
    except OSError:
        return False

    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(datetime.now(timezone.utc).isoformat() + "\n")
        subprocess.Popen(
            [str(CLAUDE_USAGE_SCRIPT), "--json", "--no-cache"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_subprocess_env(),
            cwd=str(REPO_ROOT),
            start_new_session=True,
        )
    except OSError:
        CLAUDE_USAGE_REFRESH_REQUEST_FILE.unlink(missing_ok=True)
        return False

    return True


def _assumed_claude_code_entries(reason: str) -> list[BackendQuota]:
    """Build assumed-available entries for both opus and sonnet.

    Every degraded path in check_claude_code_quota (script missing, no cache,
    nonzero exit, probe exception) must return entries for BOTH models. Opus and
    sonnet share the subscription, so when the usage probe is unavailable both
    are "assumed available". Returning only opus would make downstream consumers
    treat sonnet as unavailable. Mirrors the two-entry block-file early return.
    """
    return [
        BackendQuota(
            backend="claude-code",
            model=model,
            available=True,
            source="assumed",
            reason=reason,
            models_available=[model],
        )
        for model in ("opus", "sonnet")
    ]


def check_claude_code_quota(*, fast: bool = False) -> list[BackendQuota]:
    """Check Claude Code quota via check-claude-usage.sh.

    Returns quota entries for both opus and sonnet, since they share the
    subscription but have different weekly limits.

    Also checks for a rate-limit block file written by autonomous-run.sh
    when Claude Code hits the seven_day subscription limit. This block
    applies to ALL models (opus and sonnet share the subscription).
    """
    # Check for subscription-level rate limit block file first.
    # When the seven_day limit is hit, ALL models are blocked.
    rate_limit_block_file = (
        REPO_ROOT / "state" / "backend-quota" / "claude-code-rate-limited-until.txt"
    )
    rate_limit_until = _load_active_block_until(rate_limit_block_file)
    if rate_limit_until is not None:
        now = datetime.now(timezone.utc)
        resets_in = int((rate_limit_until - now).total_seconds())
        reason = (
            f"BLOCKED — subscription rate limit "
            f"(resets in {resets_in // 3600}h {(resets_in % 3600) // 60}m)"
        )
        return [
            BackendQuota(
                backend="claude-code",
                model="opus",
                available=False,
                utilization=1.0,
                resets_in_seconds=resets_in,
                source="block-file",
                reason=reason,
                models_available=["opus"],
            ),
            BackendQuota(
                backend="claude-code",
                model="sonnet",
                available=False,
                utilization=1.0,
                resets_in_seconds=resets_in,
                source="block-file",
                reason=reason,
                models_available=["sonnet"],
            ),
        ]

    if fast:
        cached = _load_cached_claude_usage()
        if cached is not None:
            data, cache_age_seconds = cached
            results = _augment_claude_results_with_subscription_state(
                _build_claude_code_results(
                    data,
                    source="cached",
                    cache_age_seconds=cache_age_seconds,
                ),
                fast=fast,
            )
            if cache_age_seconds >= _claude_usage_cache_ttl_seconds():
                refresh_requested = _request_claude_usage_refresh_in_background()
                refresh_metadata = _pending_claude_usage_refresh_metadata()
                if refresh_requested:
                    for entry in results:
                        entry.metadata["background_refresh_requested"] = True
                if refresh_metadata:
                    for entry in results:
                        entry.metadata.update(refresh_metadata)
            return results
        if not CLAUDE_USAGE_SCRIPT.exists():
            return _apply_claude_agent_sdk_credit_budget(
                _assumed_claude_code_entries("check-claude-usage.sh not found")
            )
        refresh_requested = _request_claude_usage_refresh_in_background()
        reason = "no cached Claude quota snapshot available for fast mode"
        if refresh_requested:
            reason += "; background refresh requested"
        return _apply_claude_agent_sdk_credit_budget(
            _assumed_claude_code_entries(reason)
        )

    if not CLAUDE_USAGE_SCRIPT.exists():
        return _apply_claude_agent_sdk_credit_budget(
            _assumed_claude_code_entries("check-claude-usage.sh not found")
        )

    try:
        proc = subprocess.run(
            [str(CLAUDE_USAGE_SCRIPT), "--json"],
            capture_output=True,
            env=_subprocess_env(),
            text=True,
            timeout=_probe_timeout(
                CLAUDE_USAGE_TIMEOUT_SECONDS,
                FAST_CLAUDE_USAGE_TIMEOUT_SECONDS,
                fast_mode=fast,
            ),
        )
        if proc.returncode != 0:
            detail = _sanitize_first_stderr_line(
                proc.stderr,
                fallback=f"check-claude-usage.sh failed (exit {proc.returncode})",
            )
            return _apply_claude_agent_sdk_credit_budget(
                _assumed_claude_code_entries(detail)
            )

        data = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return _apply_claude_agent_sdk_credit_budget(
            _assumed_claude_code_entries(f"quota check error: {e}")
        )

    return _augment_claude_results_with_subscription_state(
        _build_claude_code_results(data, source="api"),
        fast=fast,
    )


def _parse_reset_time(time_str: str) -> datetime:
    """Parse a reset time string into a UTC datetime.

    Handles:
    - ISO 8601 (from date --iso-8601=seconds): "2026-03-18T18:58:00+00:00"
    - Codex error format: "Mar 18th, 2026 6:58 PM"
    """
    import re as _re

    s = time_str.strip()

    # Try ISO 8601 first
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Try codex format: "Mar 18th, 2026 6:58 PM" or "Mar 18, 2026 6:58 PM"
    s_clean = _re.sub(r"(\d+)(st|nd|rd|th)", r"\1", s)
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            dt = datetime.strptime(s_clean, fmt)
            # Codex error times are local; treat as UTC for block-file purposes
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    raise ValueError(f"Cannot parse reset time: {time_str!r}")


def _load_active_block_until(block_file: Path) -> datetime | None:
    """Return an active block expiry and clean up expired/bad files."""
    if not block_file.exists():
        return None

    try:
        block_until = _parse_reset_time(block_file.read_text().strip())
    except (ValueError, OSError):
        block_file.unlink(missing_ok=True)
        return None

    now = datetime.now(timezone.utc)
    if block_until <= now:
        block_file.unlink(missing_ok=True)
        return None

    return block_until


def check_heuristic_quota(
    backend: str, model: str, models_available: list[str] | None = None
) -> BackendQuota:
    """Heuristic quota check based on recent session success/failure.

    If the last session on this backend failed recently, assume quota exhausted
    and wait before retrying. Otherwise, assume available.
    """
    if models_available is None:
        models_available = [model]

    try:
        if not SESSION_RECORDS_FILE.exists():
            # No recent sessions — assume available
            return BackendQuota(
                backend=backend,
                model=model,
                available=True,
                source="heuristic",
                reason="no session records",
                models_available=models_available,
            )

        cutoff = datetime.now(timezone.utc) - timedelta(hours=HEURISTIC_LOOKBACK_HOURS)
        last: dict[str, object] | None = None
        for line in SESSION_RECORDS_FILE.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("harness") != backend:
                continue
            rec_model = rec.get("model")
            if isinstance(rec_model, str) and rec_model and rec_model != model:
                continue

            timestamp_raw = rec.get("timestamp")
            if not isinstance(timestamp_raw, str):
                continue
            try:
                rec_time = datetime.fromisoformat(timestamp_raw)
            except ValueError:
                continue
            if rec_time.tzinfo is None:
                rec_time = rec_time.replace(tzinfo=timezone.utc)
            if rec_time < cutoff:
                continue
            last = rec

        if not last:
            return BackendQuota(
                backend=backend,
                model=model,
                available=True,
                source="heuristic",
                reason="no recent matching sessions",
                models_available=models_available,
            )

        outcome = last.get("outcome")
        timestamp_raw = last.get("timestamp")
        if outcome == "failed" and isinstance(timestamp_raw, str):
            last_time = datetime.fromisoformat(timestamp_raw)
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()

            if elapsed < HEURISTIC_COOLDOWN_AFTER_FAILURE:
                remaining = int(HEURISTIC_COOLDOWN_AFTER_FAILURE - elapsed)
                return BackendQuota(
                    backend=backend,
                    model=model,
                    available=False,
                    utilization=1.0,
                    resets_in_seconds=remaining,
                    source="heuristic",
                    reason=f"last session failed {int(elapsed)}s ago",
                    models_available=models_available,
                )

        return BackendQuota(
            backend=backend,
            model=model,
            available=True,
            source="heuristic",
            reason="last session succeeded" if outcome == "productive" else "available",
            models_available=models_available,
        )

    except Exception as e:
        # If we can't check, assume available
        return BackendQuota(
            backend=backend,
            model=model,
            available=True,
            source="assumed",
            reason=f"heuristic check failed: {e}",
            models_available=models_available,
        )


def _build_blocked_quota(
    *,
    backend: str,
    model: str,
    block_until: datetime,
    source: str,
    reason: str,
    models_available: list[str] | None = None,
) -> BackendQuota:
    """Build an unavailable quota entry from an active block file."""
    if models_available is None:
        models_available = [model]

    now = datetime.now(timezone.utc)
    resets_in = max(0, int((block_until - now).total_seconds()))
    return BackendQuota(
        backend=backend,
        model=model,
        available=False,
        utilization=1.0,
        resets_in_seconds=resets_in,
        source=source,
        reason=reason.format(hours=resets_in // 3600, minutes=(resets_in % 3600) // 60),
        models_available=models_available,
    )


COPILOT_USAGE_SCRIPT = REPO_ROOT / "scripts" / "check-copilot-usage.sh"

# LM Studio local inference endpoint (erb-s1-win via LM Studio Link)
LMSTUDIO_BASE_URL = "http://localhost:1234"


def check_local_model_quota(model: str, *, fast: bool = False) -> BackendQuota:
    """Check if a local model is available by pinging the LM Studio API."""
    import urllib.error
    import urllib.request

    # Check for crash-loop block file first
    block_file = BACKEND_QUOTA_DIR / f"gptme-{model}-crash-loop-until.txt"
    crash_block = _load_active_block_until(block_file)
    if crash_block is not None:
        return _build_blocked_quota(
            backend="gptme",
            model=model,
            block_until=crash_block,
            source="crash-loop-block-file",
            reason="crash loop cooldown — resets in {hours}h {minutes}m",
            models_available=[model],
        )

    try:
        req = urllib.request.Request(
            f"{LMSTUDIO_BASE_URL}/v1/models",
            method="GET",
        )
        with urllib.request.urlopen(
            req,
            timeout=_probe_timeout(
                LOCAL_MODEL_TIMEOUT_SECONDS,
                FAST_LOCAL_MODEL_TIMEOUT_SECONDS,
                fast_mode=fast,
            ),
        ) as resp:
            if resp.status == 200:
                # A 200 only proves the LM Studio server is reachable — NOT that
                # the requested model is loaded. LM Studio reports fully-qualified
                # ids (e.g. "qwen/qwen3.6-35b-a3b") while local_models() asks for a
                # short alias ("qwen3.6"), so check the model is actually present
                # before claiming it available. Otherwise the selector routes to a
                # "reachable" model that 404s at call time.
                loaded_ids: list[str] = []
                try:
                    payload = json.loads(resp.read().decode("utf-8"))
                    loaded_ids = [
                        str(m.get("id", ""))
                        for m in payload.get("data", [])
                        if isinstance(m, dict)
                    ]
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    loaded_ids = []
                model_loaded = any(
                    model == mid or model in mid for mid in loaded_ids if mid
                )
                if model_loaded:
                    return BackendQuota(
                        backend="gptme",
                        model=model,
                        available=True,
                        utilization=0.0,
                        source="lmstudio-api",
                        reason="LM Studio reachable, model loaded",
                        models_available=[model],
                    )
                return BackendQuota(
                    backend="gptme",
                    model=model,
                    available=False,
                    utilization=0.0,
                    source="lmstudio-api",
                    reason=f"LM Studio reachable but model '{model}' not loaded",
                    models_available=[model],
                )
    except (urllib.error.URLError, OSError, TimeoutError):
        pass

    return BackendQuota(
        backend="gptme",
        model=model,
        available=False,
        utilization=0.0,
        source="lmstudio-api",
        reason="LM Studio unreachable at localhost:1234",
        models_available=[model],
    )


def _find_grok_build_binary() -> Path | None:
    """Return the Grok Build CLI path when installed."""
    path = shutil.which("grok")
    if path:
        return Path(path)

    fallback = Path.home() / ".grok" / "bin" / "grok"
    if fallback.is_file():
        return fallback
    return None


def check_grok_build_quota() -> BackendQuota:
    """Check Grok Build CLI availability via install + OAuth token presence."""
    model = "grok-build"
    binary = _find_grok_build_binary()
    if binary is None:
        return BackendQuota(
            backend="grok-build",
            model=model,
            available=False,
            source="missing-binary",
            reason="grok CLI not installed",
        )

    if not GROK_BUILD_AUTH_FILE.is_file():
        return BackendQuota(
            backend="grok-build",
            model=model,
            available=False,
            source="missing-auth",
            reason="~/.grok/auth.json missing — run grok login --device-auth",
        )

    return BackendQuota(
        backend="grok-build",
        model=model,
        available=True,
        utilization=0.0,
        source="local-cli",
        reason=f"installed + authenticated via {binary}",
        models_available=[model],
        metadata={"auth_file": str(GROK_BUILD_AUTH_FILE)},
    )


# Treat copilot as exhausted when <10% of monthly premium pool remains.
# With a 300-request monthly cap, 10% = 30 requests = ~1 day of reserve for
# emergency use if the Claude Max pool dies mid-session.
COPILOT_EXHAUSTED_BELOW_PCT = 0.10


def check_copilot_quota(*, fast: bool = False) -> list[BackendQuota]:
    """Query Copilot Pro premium-request quota via the internal user endpoint.

    NOTE: All copilot-cli models (opus, sonnet, gpt-5.4) share ONE monthly
    premium-request pool. Identical utilization values across models is the
    expected, correct behavior — not a quota-reading artifact.
    """
    models = ["claude-opus-4.6", "claude-sonnet-4.6", "gpt-5.4"]
    if not COPILOT_USAGE_SCRIPT.exists():
        return [
            BackendQuota(
                backend="copilot-cli",
                model=m,
                available=False,
                source="missing-script",
                reason="check-copilot-usage.sh not present",
            )
            for m in models
        ]

    try:
        result = subprocess.run(
            ["bash", str(COPILOT_USAGE_SCRIPT)],
            capture_output=True,
            env=_subprocess_env(),
            text=True,
            timeout=_probe_timeout(
                COPILOT_USAGE_TIMEOUT_SECONDS,
                FAST_COPILOT_USAGE_TIMEOUT_SECONDS,
                fast_mode=fast,
            ),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return [
            BackendQuota(
                backend="copilot-cli",
                model=m,
                available=False,
                source="api-error",
                reason=f"copilot usage fetch failed: {e}",
            )
            for m in models
        ]

    if result.returncode != 0 or not result.stdout.strip():
        reason = _sanitize_first_stderr_line(
            result.stderr, fallback="copilot usage non-zero exit"
        )
        return [
            BackendQuota(
                backend="copilot-cli",
                model=m,
                available=False,
                source="api-error",
                reason=reason,
            )
            for m in models
        ]

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [
            BackendQuota(
                backend="copilot-cli",
                model=m,
                available=False,
                source="api-error",
                reason="malformed JSON from copilot usage script",
            )
            for m in models
        ]

    pct_remaining = data.get("premium_pct")
    unlimited = bool(data.get("premium_unlimited"))
    metadata = {
        key: value
        for key, value in {
            "plan": data.get("plan"),
            "sku": data.get("sku"),
        }.items()
        if value is not None
    }
    if pct_remaining is None:
        pct_remaining = 1.0

    resets_in_seconds = 0
    reset_date = data.get("reset_date")
    if isinstance(reset_date, str) and reset_date.strip():
        try:
            reset_dt = _parse_reset_time(reset_date)
            resets_in_seconds = max(
                0, int((reset_dt - datetime.now(timezone.utc)).total_seconds())
            )
        except ValueError:
            resets_in_seconds = 0

    utilization = 1.0 - pct_remaining
    available = unlimited or pct_remaining >= COPILOT_EXHAUSTED_BELOW_PCT
    reason = ""
    if not available:
        remaining_n = data.get("premium_remaining", 0)
        reset = data.get("reset_date", "?")
        reason = (
            f"Copilot premium pool low: {remaining_n} requests left "
            f"(<{int(COPILOT_EXHAUSTED_BELOW_PCT * 100)}% of monthly cap), "
            f"resets {reset}"
        )
        # Persist a subscription-wide block file so plateau_detector and other
        # static filters can see copilot as exhausted without re-fetching live
        # quota. All copilot-cli arms share one premium pool.
        if isinstance(reset_date, str) and reset_date.strip():
            try:
                reset_dt = _parse_reset_time(reset_date)
                BACKEND_QUOTA_DIR.mkdir(parents=True, exist_ok=True)
                (BACKEND_QUOTA_DIR / "copilot-cli-blocked-until.txt").write_text(
                    reset_dt.isoformat()
                )
            except (ValueError, OSError):
                pass
    else:
        # Clean up any stale subscription-wide block file when copilot becomes
        # available again (premium pool refilled or reset arrived).
        block_file = BACKEND_QUOTA_DIR / "copilot-cli-blocked-until.txt"
        if block_file.exists():
            try:
                block_file.unlink()
            except OSError:
                pass

    return [
        BackendQuota(
            backend="copilot-cli",
            model=m,
            available=available,
            utilization=utilization,
            resets_in_seconds=resets_in_seconds,
            source="api",
            reason=reason,
            models_available=[m] if available else [],
            metadata=metadata,
        )
        for m in models
    ]


def check_openrouter_quota(context: str = "", *, fast: bool = False) -> dict:
    """Check OpenRouter API key usage via check-openrouter-usage.sh.

    When ``context`` is set, the helper script picks the context-scoped key
    (e.g. ``autonomous_deepseek``) so models with their own dedicated daily
    budget aren't marked unavailable by an exhausted shared autonomous key.
    """
    if not OPENROUTER_USAGE_SCRIPT.exists():
        return {
            "available": True,
            "utilization": 0,
            "source": "assumed",
            "reason": "script not found",
        }
    cmd = [str(OPENROUTER_USAGE_SCRIPT), "--json"]
    if context:
        cmd.extend(["--context", context])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            env=_subprocess_env(),
            text=True,
            timeout=_probe_timeout(
                OPENROUTER_USAGE_TIMEOUT_SECONDS,
                FAST_OPENROUTER_USAGE_TIMEOUT_SECONDS,
                fast_mode=fast,
            ),
        )
        if result.returncode != 0:
            return {
                "available": True,
                "utilization": 0,
                "source": "assumed",
                "reason": "script failed",
            }
        data = json.loads(result.stdout)
        return {
            "available": data.get("available", True),
            "utilization": data.get("utilization", 0),
            "source": "api",
            "reason": f"${data.get('usage_daily', 0):.2f}/${data.get('limit', '?')} daily",
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return {
            "available": True,
            "utilization": 0,
            "source": "assumed",
            "reason": str(e),
        }


XAI_PROBE_MODEL = "grok-3-mini"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
XAI_PROBE_TIMEOUT = 8  # seconds


def _get_xai_api_key() -> str | None:
    """Read XAI_API_KEY from environment or gptme config."""
    key = os.environ.get("XAI_API_KEY")
    if key:
        return key
    try:
        from gptme.config import get_config

        return get_config().get_env("XAI_API_KEY")
    except Exception:
        return None


def check_xai_credits() -> BackendQuota:
    """Probe xAI chat completions API to detect credit exhaustion.

    xAI does not expose a balance/billing API, so we make a minimal
    1-token chat completion call as a liveness + credit probe.

    HTTP 200 → credits available.
    HTTP 429 → credit exhaustion or rate limit (check error body).
    Other errors → assume available (transient, don't block standup calls).
    """
    import urllib.error
    import urllib.request

    api_key = _get_xai_api_key()
    if not api_key:
        return BackendQuota(
            backend="xai",
            model=XAI_PROBE_MODEL,
            available=True,  # Can't check → assume OK
            source="assumed",
            reason="XAI_API_KEY not configured",
        )

    payload = json.dumps(
        {
            "model": XAI_PROBE_MODEL,
            "messages": [{"role": "user", "content": "1"}],
            "max_tokens": 1,
        }
    ).encode()

    req = urllib.request.Request(
        XAI_CHAT_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=XAI_PROBE_TIMEOUT) as resp:
            body = json.loads(resp.read())
        cost_ticks = None
        if isinstance(body, dict):
            usage = body.get("usage", {})
            if isinstance(usage, dict):
                cost_ticks = usage.get("cost_in_usd_ticks")
        metadata: dict[str, str | int | float | bool | None] = {}
        if cost_ticks is not None:
            metadata["probe_cost_usd_ticks"] = cost_ticks
        return BackendQuota(
            backend="xai",
            model=XAI_PROBE_MODEL,
            available=True,
            utilization=0.0,
            source="api",
            reason="",
            metadata=metadata,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                error_msg = (
                    json.loads(exc.read().decode()).get("error", {}).get("message", "")
                    or ""
                )
            except Exception:
                error_msg = ""
            exhausted = not error_msg or any(
                kw in error_msg.lower()
                for kw in ("insufficient", "credit", "balance", "quota", "exceeded")
            )
            if exhausted:
                return BackendQuota(
                    backend="xai",
                    model=XAI_PROBE_MODEL,
                    available=False,
                    source="api",
                    reason=f"xAI 429 — credit exhaustion suspected: {error_msg or 'no detail in response'}",
                )
            # Distinguishable rate limit (temporary) — don't block
            return BackendQuota(
                backend="xai",
                model=XAI_PROBE_MODEL,
                available=True,
                source="api",
                reason=f"xAI rate limited (temporary): {error_msg}",
            )
        # Other HTTP errors (5xx, etc.) — assume available
        return BackendQuota(
            backend="xai",
            model=XAI_PROBE_MODEL,
            available=True,
            source="api",
            reason=f"xAI probe HTTP {exc.code} (transient, assuming available)",
        )
    except Exception as exc:
        # Network error, timeout, etc. — assume available (don't block standup)
        return BackendQuota(
            backend="xai",
            model=XAI_PROBE_MODEL,
            available=True,
            source="assumed",
            reason=f"xAI probe failed (transient): {exc}",
        )


def _scan_crash_loop_blocks(
    backend: str | None = None,
) -> list[tuple[str, str, datetime]]:
    """Scan for active crash-loop block files written by autonomous-run.sh.

    Files follow: state/backend-quota/{backend}-{model}-crash-loop-until.txt
    Known backends are used to split the filename into backend + model parts.

    Args:
        backend: If set, only return blocks for this backend.
    Returns:
        List of (backend, model, block_until) tuples for active blocks.
    """
    known_backends = ["claude-code", "gptme", "codex", "copilot-cli", "grok-build"]
    results: list[tuple[str, str, datetime]] = []
    suffix = "-crash-loop-until.txt"

    for block_file in sorted(BACKEND_QUOTA_DIR.glob(f"*{suffix}")):
        block_until = _load_active_block_until(block_file)
        if not block_until:
            continue
        # Strip suffix to get "{backend}-{model}"
        prefix = block_file.name[: -len(suffix)]
        # Disambiguate backend from model using known prefixes
        parsed_backend: str | None = None
        parsed_model: str | None = None
        for kb in known_backends:
            if prefix.startswith(kb + "-"):
                parsed_backend = kb
                parsed_model = prefix[len(kb) + 1 :]
                break
        if parsed_backend is None:
            continue  # Unknown format — skip
        if backend and parsed_backend != backend:
            continue
        results.append((parsed_backend, parsed_model or "unknown", block_until))

    return results


# Default backend set. Per-agent availability is configuration: an agent reports
# only the backends it actually has (bob: claude-code + gptme + codex + openrouter
# + local; alice/gordon: claude-code only). Override via the QUOTA_BACKENDS env
# var (comma-separated) or the explicit `backends=` arg. Each per-backend probe
# still degrades gracefully (missing creds/key/endpoint → "unavailable"), so an
# over-broad set is safe; QUOTA_BACKENDS just trims noise to the configured set.
DEFAULT_BACKENDS = ["claude-code", "gptme", "codex", "copilot-cli", "grok-build"]


def configured_backends() -> list[str]:
    """Resolve the agent's available backends from config (QUOTA_BACKENDS env)."""
    raw = os.environ.get("QUOTA_BACKENDS", "").strip()
    if raw:
        seen: set[str] = set()
        result: list[str] = []
        for b in raw.split(","):
            b = b.strip()
            if b and b not in seen:
                seen.add(b)
                result.append(b)
        return result
    return list(DEFAULT_BACKENDS)


def check_all_quotas(
    backends: list[str] | None = None, *, fast: bool = False
) -> QuotaReport:
    """Check quota across the agent's configured backends.

    Args:
        backends: Explicit list to check. None = the agent's configured set
            (QUOTA_BACKENDS env var, else DEFAULT_BACKENDS).
        fast: Use shorter live-probe timeouts for dashboard/status surfaces.
    """
    all_backends = backends or configured_backends()
    results: list[BackendQuota] = []
    errors: list[str] = []

    for backend in all_backends:
        try:
            if backend == "claude-code":
                results.extend(check_claude_code_quota(fast=fast))
            elif backend == "gptme":
                # gpt-5.4 and gpt-5.5 both use the ChatGPT subscription
                for model in ("gpt-5.4", "gpt-5.5"):
                    gptme_block = _load_active_block_until(
                        BACKEND_QUOTA_DIR / f"gptme-{model}-blocked-until.txt"
                    )
                    crash_loop_block = _load_active_block_until(
                        BACKEND_QUOTA_DIR / f"gptme-{model}-crash-loop-until.txt"
                    )
                    shared_block = max(
                        filter(
                            None,
                            [gptme_block, crash_loop_block],
                        ),
                        default=None,
                    )
                    if shared_block is not None:
                        # Distinguish crash loop from quota exhaustion in reason
                        if crash_loop_block and shared_block == crash_loop_block:
                            results.append(
                                _build_blocked_quota(
                                    backend="gptme",
                                    model=model,
                                    block_until=shared_block,
                                    source="crash-loop-block-file",
                                    reason="crash loop cooldown — resets in {hours}h {minutes}m",
                                )
                            )
                        else:
                            results.append(
                                _build_blocked_quota(
                                    backend="gptme",
                                    model=model,
                                    block_until=shared_block,
                                    source="block-file",
                                    reason="ChatGPT subscription exhausted — resets in {hours}h {minutes}m",
                                )
                            )
                    else:
                        # gpt-5.4/5.5 share the ChatGPT Codex pool with codex:*.
                        # Prefer the scraped /status usage (the SAME cache the
                        # codex backend reads), so a weekly-exhausted pool blocks
                        # the gptme openai-subscription models too — not just
                        # codex:*. This closes the blind spot where codex:gpt-5.5
                        # showed exhausted but gptme:gpt-5.5 stayed "heuristic
                        # available" and kept 429-ing on the shared pool.
                        result = _shared_codex_pool_quota("gptme", model)
                        if result is None:
                            # Cache stale/missing: fall back to the crash heuristic,
                            # then cross-reference a codex block file (block OR
                            # rate-limit suffix) as a secondary exhaustion signal.
                            result = check_heuristic_quota("gptme", model, [model])
                            if result.available:
                                codex_block = max(
                                    filter(
                                        None,
                                        [
                                            _load_active_block_until(
                                                BACKEND_QUOTA_DIR
                                                / f"codex-{model}-blocked-until.txt"
                                            ),
                                            _load_active_block_until(
                                                BACKEND_QUOTA_DIR
                                                / f"codex-{model}-rate-limited-until.txt"
                                            ),
                                        ],
                                    ),
                                    default=None,
                                )
                                if codex_block is not None:
                                    result = _build_blocked_quota(
                                        backend="gptme",
                                        model=model,
                                        block_until=codex_block,
                                        source="cross-ref:codex-block-file",
                                        reason="ChatGPT Codex subscription exhausted (shared pool) — resets in {hours}h {minutes}m",
                                    )
                        results.append(result)
                # OpenRouter models — each may use a context-scoped key with
                # its own daily $ budget (e.g. deepseek has a dedicated key).
                # Cache per-context lookups so we don't re-shell-out per model.
                or_quota_by_context: dict[str, dict] = {}
                or_models = openrouter_models(QUOTA_CONFIG)
                for model in or_models:
                    ctx = gptme_openrouter_context(model, QUOTA_CONFIG)
                    if ctx not in or_quota_by_context:
                        or_quota_by_context[ctx] = check_openrouter_quota(
                            ctx, fast=fast
                        )
                    or_quota = or_quota_by_context[ctx]
                    results.append(
                        BackendQuota(
                            backend="gptme",
                            model=model,
                            available=or_quota.get("available", True),
                            utilization=or_quota.get("utilization", 0),
                            source=or_quota.get("source", "assumed"),
                            reason=or_quota.get("reason", ""),
                            models_available=[model],
                        )
                    )
                # Local models (LM Studio) — zero cost, availability via API ping
                for model in local_models(QUOTA_CONFIG):
                    results.append(check_local_model_quota(model, fast=fast))
            elif backend == "codex":
                # gpt-5.4 and gpt-5.5 both use the ChatGPT Codex subscription
                for model in ("gpt-5.4", "gpt-5.5"):
                    codex_retry_loop_block = _load_active_block_until(
                        BACKEND_QUOTA_DIR / f"codex-{model}-retry-loop-until.txt"
                    )
                    codex_quota_block = _load_active_block_until(
                        BACKEND_QUOTA_DIR / f"codex-{model}-blocked-until.txt"
                    )
                    shared_block = max(
                        filter(None, [codex_retry_loop_block, codex_quota_block]),
                        default=None,
                    )
                    if shared_block is not None:
                        if codex_quota_block and shared_block == codex_quota_block:
                            results.append(
                                _build_blocked_quota(
                                    backend="codex",
                                    model=model,
                                    block_until=shared_block,
                                    source="block-file",
                                    reason="Codex quota exhausted — resets in {hours}h {minutes}m",
                                )
                            )
                        else:
                            results.append(
                                _build_blocked_quota(
                                    backend="codex",
                                    model=model,
                                    block_until=shared_block,
                                    source="retry-loop-block-file",
                                    reason="retry loop cooldown — resets in {hours}h {minutes}m",
                                )
                            )
                    else:
                        # No block file: prefer the scraped /status usage (pacing-
                        # aware, proactive) and fall back to the crash heuristic.
                        results.append(_build_codex_quota_from_usage(model))
            elif backend == "copilot-cli":
                results.extend(check_copilot_quota(fast=fast))
            elif backend == "grok-build":
                grok_block = _load_active_block_until(
                    BACKEND_QUOTA_DIR / "grok-build-grok-build-blocked-until.txt"
                )
                crash_loop_block = _load_active_block_until(
                    BACKEND_QUOTA_DIR / "grok-build-grok-build-crash-loop-until.txt"
                )
                shared_block = max(
                    filter(None, [grok_block, crash_loop_block]),
                    default=None,
                )
                if shared_block is not None:
                    if crash_loop_block and shared_block == crash_loop_block:
                        results.append(
                            _build_blocked_quota(
                                backend="grok-build",
                                model="grok-build",
                                block_until=shared_block,
                                source="crash-loop-block-file",
                                reason="crash loop cooldown — resets in {hours}h {minutes}m",
                            )
                        )
                    else:
                        results.append(
                            _build_blocked_quota(
                                backend="grok-build",
                                model="grok-build",
                                block_until=shared_block,
                                source="block-file",
                                reason="SuperGrok Heavy unavailable — resets in {hours}h {minutes}m",
                            )
                        )
                else:
                    results.append(check_grok_build_quota())
            else:
                errors.append(f"Unknown backend: {backend}")
        except Exception as e:
            errors.append(f"{backend}: {e}")

    # Supplement per-backend checks with any crash-loop blocks written by
    # autonomous-run.sh that the per-backend sections above might not catch.
    # Build an index over ALL evaluated results (not just unavailable ones) so
    # we can override an available=True entry when a crash-loop block file exists
    # for the same model — without creating a second conflicting row.
    evaluated_idx = {(r.backend, r.model): i for i, r in enumerate(results)}
    explicit_backend_filter = set(all_backends) if backends is not None else None
    for be, model, block_until in _scan_crash_loop_blocks():
        if explicit_backend_filter is not None and be not in explicit_backend_filter:
            continue
        blocked = _build_blocked_quota(
            backend=be,
            model=model,
            block_until=block_until,
            source="crash-loop-block-file",
            reason="crash loop cooldown — resets in {hours}h {minutes}m",
        )
        idx = evaluated_idx.get((be, model))
        if idx is not None:
            if results[idx].available:
                # Per-backend check said available; crash-loop block overrides it.
                results[idx] = blocked
            # else: already marked unavailable — per-backend check covers it, skip.
        else:
            results.append(blocked)

    return QuotaReport(
        backends=results,
        timestamp=datetime.now(timezone.utc).isoformat(),
        errors=errors,
    )


def print_pretty(report: QuotaReport) -> None:
    """Print human-readable quota report."""
    print("Cross-Harness Quota Report")
    print("=" * 60)
    print(f"  Timestamp: {report.timestamp}")

    # Warn if approaching or past the Agent SDK credit change date
    now = datetime.now(timezone.utc)
    if is_post_agent_sdk_credit_change(now):
        print(
            f"\n  ⚠️  POST-JUNE-15 MODE: claude-code draws from ${CLAUDE_AGENT_SDK_ASSUMED_MONTHLY_CREDIT_USD:.0f}/mo"
            " Agent SDK credit pool (not subscription limits)."
        )
    else:
        days_left = (CLAUDE_AGENT_SDK_CREDIT_CHANGE_DATE - now).days
        if days_left <= 30:
            print(
                f"\n  ℹ️  {days_left}d until Agent SDK credit change (June 15):"
                f" claude-code switches to ${CLAUDE_AGENT_SDK_ASSUMED_MONTHLY_CREDIT_USD:.0f}/mo credit pool."
                " See ErikBjare/bob#786."
            )
    print()

    for bq in report.backends:
        status = "✓ available" if bq.available else "✗ exhausted"
        bar_width = 20
        filled = int(bq.utilization * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)

        print(f"  {bq.backend}:{bq.model}")
        print(f"    Status:  {status}")
        if bq.source == "api":
            print(f"    Usage:   [{bar}] {bq.utilization:.0%}")
        else:
            print(f"    Source:  {bq.source} ({bq.reason})")
        if bq.resets_in_seconds > 0:
            secs = bq.resets_in_seconds
            if secs >= 86400:
                time_str = f"{secs / 86400:.1f}d"
            elif secs >= 3600:
                time_str = f"{secs / 3600:.1f}h"
            else:
                time_str = f"{secs // 60}m"
            pace = None
            if bq.pacing_status == "overusing":
                pace = "▲ ahead"
            elif bq.pacing_status == "underusing":
                pace = "▽ behind"
            elif bq.pacing_status == "on_track":
                pace = "● on track"
            elif bq.pacing_status == "blocked":
                pace = "■ blocked"
            print(f"    Resets:  {time_str}{f' ({pace})' if pace else ''}")
        if bq.pacing_target is not None and bq.pacing_gap is not None:
            print(
                f"    Pace:    target {bq.pacing_target:.0%}, gap {bq.pacing_gap:+.0%}"
            )
        if bq.reason:
            print(f"    Detail: {bq.reason}")
        if bq.metadata:
            metadata = ", ".join(
                f"{key}={value}" for key, value in sorted(bq.metadata.items())
            )
            print(f"    Meta:   {metadata}")
        print()

    if report.errors:
        print("Errors:")
        for err in report.errors:
            print(f"  - {err}")
        print()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check quota across all backends")
    parser.add_argument("--pretty", action="store_true", help="Human-readable output")
    parser.add_argument("--json", action="store_true", help="JSON output (default)")
    parser.add_argument(
        "--backend",
        action="append",
        help="Check specific backend(s) only (can be repeated)",
    )
    parser.add_argument(
        "--xai",
        action="store_true",
        help="Also probe xAI credit availability (voice provider, not a harness backend)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use shorter live-probe timeouts for dashboard and health-check surfaces",
    )
    args = parser.parse_args()

    report = check_all_quotas(backends=args.backend, fast=args.fast)

    xai_quota: BackendQuota | None = None
    if args.xai:
        xai_quota = check_xai_credits()

    if args.pretty:
        print_pretty(report)
        if xai_quota is not None:
            print("Voice Providers")
            print("=" * 60)
            status = "✓ available" if xai_quota.available else "✗ exhausted"
            print(f"  xai:{xai_quota.model}")
            print(f"    Status: {status}")
            if xai_quota.source == "api":
                print("    Source: live probe")
            if xai_quota.reason:
                print(f"    Detail: {xai_quota.reason}")
            if xai_quota.metadata:
                meta = ", ".join(
                    f"{k}={v}" for k, v in sorted(xai_quota.metadata.items())
                )
                print(f"    Meta:   {meta}")
            print()
    else:
        out = json.loads(report.to_json())
        if xai_quota is not None:
            from dataclasses import asdict

            out["xai"] = asdict(xai_quota)
        print(json.dumps(out, indent=2))

    # Exit non-zero if NO backends are available
    if not any(bq.available for bq in report.backends):
        return 1
    # Exit non-zero if xAI is explicitly checked and unavailable
    if xai_quota is not None and not xai_quota.available:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
