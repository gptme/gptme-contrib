"""
Parse gptme conversation logs to extract session metadata.

Delegates directory scanning and token extraction to gptme-sessions,
keeping only aggregation types and formatting here.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from gptme_sessions.discovery import discover_gptme_sessions, parse_gptme_config
from gptme_sessions.signals import extract_usage_gptme, parse_trajectory

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    """Metadata for a single gptme session."""

    name: str
    model: str = ""
    harness: str = ""  # "gptme", "claude-code", "codex", etc.
    workspace: str = ""
    message_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    duration_seconds: float = 0.0
    interactive: bool = True


@dataclass
class ModelBreakdown:
    """Per-model (and optionally per-harness) aggregated usage."""

    model: str
    harness: str = ""
    sessions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class SessionStats:
    """Aggregated session stats for a date range."""

    start_date: date
    end_date: date
    session_count: int = 0
    models_used: dict[str, int] = field(default_factory=dict)  # model -> count
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    total_duration_seconds: float = 0.0
    sessions: list[SessionInfo] = field(default_factory=list)
    _model_data: dict[str, ModelBreakdown] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def model_breakdown(self) -> list[ModelBreakdown]:
        """Per-model usage breakdown, sorted by session count descending."""
        return sorted(self._model_data.values(), key=lambda m: -m.sessions)


def _extract_duration(msgs: list[dict]) -> float:
    """Extract session duration from first/last message timestamps."""
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    for msg in msgs:
        ts_str = msg.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        except (ValueError, TypeError):
            pass
    if first_ts and last_ts:
        return max(0.0, (last_ts - first_ts).total_seconds())
    return 0.0


def _aggregate_session(stats: SessionStats, info: SessionInfo) -> None:
    """Add a single session's data into aggregated stats."""
    stats.session_count += 1
    stats.sessions.append(info)

    if info.model:
        stats.models_used[info.model] = stats.models_used.get(info.model, 0) + 1
        # Per harness+model breakdown
        key = f"{info.harness}/{info.model}" if info.harness else info.model
        if key not in stats._model_data:
            stats._model_data[key] = ModelBreakdown(model=info.model, harness=info.harness)
        mb = stats._model_data[key]
        mb.sessions += 1
        mb.input_tokens += info.input_tokens
        mb.output_tokens += info.output_tokens
        mb.cost += info.cost

    stats.total_input_tokens += info.input_tokens
    stats.total_output_tokens += info.output_tokens
    stats.total_cost += info.cost
    stats.total_duration_seconds += info.duration_seconds


def fetch_session_stats(
    target_date: date,
    logs_dir: Path | None = None,
) -> SessionStats:
    """Fetch session stats for a single date."""
    return fetch_session_stats_range(target_date, target_date, logs_dir)


def fetch_session_stats_range(
    start: date,
    end: date,
    logs_dir: Path | None = None,
) -> SessionStats:
    """Fetch aggregated session stats for a date range.

    Uses gptme-sessions for directory discovery, config parsing, and
    token extraction. Handles timestamps/duration locally.
    """
    stats = SessionStats(start_date=start, end_date=end)

    for session_dir in discover_gptme_sessions(start, end, logs_dir):
        info = SessionInfo(name=session_dir.name, harness="gptme")

        # Config (model, workspace, interactive) via gptme-sessions
        config = parse_gptme_config(session_dir)
        info.model = config["model"]
        info.workspace = config["workspace"]
        info.interactive = config["interactive"]

        # Parse conversation for usage + duration
        conv_path = session_dir / "conversation.jsonl"
        if conv_path.exists():
            msgs = parse_trajectory(conv_path)
            info.message_count = len(msgs)
            info.duration_seconds = _extract_duration(msgs)

            # Token usage via gptme-sessions
            usage = extract_usage_gptme(msgs)
            if usage:
                info.model = info.model or usage.get("model") or ""
                info.input_tokens = usage["input_tokens"]
                info.output_tokens = usage["output_tokens"]
                info.cost = usage["cost"]

        _aggregate_session(stats, info)

    return stats


def merge_session_stats(a: SessionStats, b: SessionStats) -> SessionStats:
    """Merge two SessionStats objects into one.

    Sums tokens, costs, durations; merges model breakdowns; concatenates session lists.
    Uses the earliest start_date and latest end_date from either input.
    """
    merged = SessionStats(
        start_date=min(a.start_date, b.start_date),
        end_date=max(a.end_date, b.end_date),
        session_count=a.session_count + b.session_count,
        total_input_tokens=a.total_input_tokens + b.total_input_tokens,
        total_output_tokens=a.total_output_tokens + b.total_output_tokens,
        total_cost=a.total_cost + b.total_cost,
        total_duration_seconds=a.total_duration_seconds + b.total_duration_seconds,
        sessions=a.sessions + b.sessions,
    )

    # Merge models_used counts
    for model, count in a.models_used.items():
        merged.models_used[model] = merged.models_used.get(model, 0) + count
    for model, count in b.models_used.items():
        merged.models_used[model] = merged.models_used.get(model, 0) + count

    # Merge model breakdown data (keys are "harness/model" composites)
    for source in (a, b):
        for key, mb in source._model_data.items():
            if key not in merged._model_data:
                merged._model_data[key] = ModelBreakdown(model=mb.model, harness=mb.harness)
            dest = merged._model_data[key]
            dest.sessions += mb.sessions
            dest.input_tokens += mb.input_tokens
            dest.output_tokens += mb.output_tokens
            dest.cost += mb.cost

    return merged


def format_sessions_for_prompt(stats: SessionStats) -> str:
    """Format session stats as markdown for injection into LLM prompts.

    Returns empty string if no session data.
    """
    if stats.session_count == 0:
        return ""

    lines: list[str] = []
    lines.append("## Session Data (Real Data)")
    lines.append(f"Period: {stats.start_date.isoformat()} to {stats.end_date.isoformat()}")
    lines.append(f"- **Sessions**: {stats.session_count}")

    if stats.models_used:
        models_str = ", ".join(
            f"{model} ({count}x)"
            for model, count in sorted(stats.models_used.items(), key=lambda x: -x[1])
        )
        lines.append(f"- **Models used**: {models_str}")

    if stats.total_tokens > 0:
        lines.append(
            f"- **Total tokens**: {stats.total_tokens:,} (input: {stats.total_input_tokens:,}, output: {stats.total_output_tokens:,})"
        )

    if stats.total_cost > 0:
        lines.append(f"- **Total cost**: ${stats.total_cost:.2f}")

    if stats.total_duration_seconds > 0:
        hours = stats.total_duration_seconds / 3600
        lines.append(f"- **Total session time**: {hours:.1f}h")

    lines.append("")
    return "\n".join(lines)
