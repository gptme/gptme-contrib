"""SessionStore — append-only JSONL persistence for session records."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .record import SessionRecord


class SessionStore:
    """Append-only JSONL store for session records.

    Args:
        sessions_dir: Directory containing the sessions file.
            Defaults to ``./state/sessions`` relative to cwd.
        sessions_file: Name of the JSONL file within sessions_dir.
    """

    def __init__(
        self,
        sessions_dir: Path | None = None,
        sessions_file: str = "session-records.jsonl",
    ):
        if sessions_dir is None:
            sessions_dir = Path.cwd() / "state" / "sessions"
        self.sessions_dir = sessions_dir
        self.sessions_file = sessions_file
        self.path = sessions_dir / sessions_file

    def append(self, record: SessionRecord) -> Path:
        """Append a session record to the JSONL store."""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(record.to_json() + "\n")
        return self.path

    def load_all(self) -> list[SessionRecord]:
        """Load all session records from the JSONL store."""
        if not self.path.exists():
            return []
        records = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(SessionRecord.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        continue
        return records

    def rewrite(self, records: list[SessionRecord]) -> Path:
        """Atomically rewrite the JSONL store with updated records."""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w") as f:
            for record in records:
                f.write(record.to_json() + "\n")
        tmp_path.rename(self.path)
        return self.path

    def query(
        self,
        model: str | None = None,
        run_type: str | None = None,
        category: str | None = None,
        harness: str | None = None,
        outcome: str | None = None,
        since_days: int | None = None,
    ) -> list[SessionRecord]:
        """Filter session records by criteria."""
        records = self.load_all()
        if model:
            records = [r for r in records if r.model == model]
        if run_type:
            records = [r for r in records if r.run_type == run_type]
        if category:
            records = [r for r in records if r.category == category]
        if harness:
            records = [r for r in records if r.harness == harness]
        if outcome:
            records = [r for r in records if r.outcome == outcome]
        if since_days is not None:
            cutoff = datetime.now(timezone.utc).timestamp() - (since_days * 86400)
            filtered = []
            for r in records:
                try:
                    ts = datetime.fromisoformat(r.timestamp).timestamp()
                    if ts >= cutoff:
                        filtered.append(r)
                except (ValueError, TypeError):
                    continue
            records = filtered
        return records

    def stats(
        self,
        records: list[SessionRecord] | None = None,
    ) -> dict:
        """Compute summary statistics from session records."""
        if records is None:
            records = self.load_all()
        if not records:
            return {"total": 0}

        total = len(records)
        productive = sum(1 for r in records if r.outcome == "productive")
        noop = sum(1 for r in records if r.outcome == "noop")

        # Model breakdown
        model_stats: dict[str, dict[str, int]] = {}
        for r in records:
            if r.model not in model_stats:
                model_stats[r.model] = {"total": 0, "productive": 0}
            model_stats[r.model]["total"] += 1
            if r.outcome == "productive":
                model_stats[r.model]["productive"] += 1

        # Run-type breakdown
        run_type_stats: dict[str, dict[str, int]] = {}
        for r in records:
            if r.run_type not in run_type_stats:
                run_type_stats[r.run_type] = {"total": 0, "productive": 0}
            run_type_stats[r.run_type]["total"] += 1
            if r.outcome == "productive":
                run_type_stats[r.run_type]["productive"] += 1

        # Model × run-type cross-tab
        cross_tab: dict[str, dict[str, int]] = {}
        for r in records:
            key = f"{r.model}×{r.run_type}"
            if key not in cross_tab:
                cross_tab[key] = {"total": 0, "productive": 0}
            cross_tab[key]["total"] += 1
            if r.outcome == "productive":
                cross_tab[key]["productive"] += 1

        # Harness breakdown
        harness_stats: dict[str, dict[str, int]] = {}
        for r in records:
            if r.harness not in harness_stats:
                harness_stats[r.harness] = {"total": 0, "productive": 0}
            harness_stats[r.harness]["total"] += 1
            if r.outcome == "productive":
                harness_stats[r.harness]["productive"] += 1

        # Harness × model cross-tab
        harness_model_tab: dict[str, dict[str, int]] = {}
        for r in records:
            key = f"{r.harness}×{r.model}"
            if key not in harness_model_tab:
                harness_model_tab[key] = {"total": 0, "productive": 0}
            harness_model_tab[key]["total"] += 1
            if r.outcome == "productive":
                harness_model_tab[key]["productive"] += 1

        # Duration stats
        durations = [r.duration_seconds for r in records if r.duration_seconds > 0]
        duration_stats: dict[str, float | int] = {}
        if durations:
            duration_stats = {
                "count": len(durations),
                "avg": sum(durations) / len(durations),
                "min": min(durations),
                "max": max(durations),
                "total_hours": sum(durations) / 3600,
            }

        def _rate(s: dict[str, int]) -> dict:
            return {**s, "rate": s["productive"] / s["total"] if s["total"] > 0 else 0}

        return {
            "total": total,
            "productive": productive,
            "noop": noop,
            "success_rate": productive / total if total > 0 else 0,
            "duration": duration_stats,
            "by_model": {m: _rate(s) for m, s in sorted(model_stats.items())},
            "by_run_type": {rt: _rate(s) for rt, s in sorted(run_type_stats.items())},
            "by_model_run_type": {k: _rate(s) for k, s in sorted(cross_tab.items())},
            "by_harness": {h: _rate(s) for h, s in sorted(harness_stats.items())},
            "by_harness_model": {k: _rate(s) for k, s in sorted(harness_model_tab.items())},
        }


def format_stats(stats: dict, out: TextIO = sys.stdout) -> None:
    """Pretty-print session statistics."""
    total = stats.get("total", 0)
    if total == 0:
        out.write("No session records found.\n")
        return

    rate = stats.get("success_rate", 0)
    out.write(f"Sessions: {total} total, {stats['productive']} productive ")
    out.write(f"({rate:.0%} success rate)\n")

    dur = stats.get("duration", {})
    if dur:
        avg_min = dur["avg"] / 60
        total_hrs = dur["total_hours"]
        out.write(
            f"Duration: {dur['count']} with data, " f"avg {avg_min:.0f}m, total {total_hrs:.1f}h\n"
        )
    out.write("\n")

    if stats.get("by_model"):
        out.write("By model:\n")
        for model, ms in stats["by_model"].items():
            out.write(
                f"  {model:12s}  {ms['productive']:3d}/{ms['total']:3d}  ({ms['rate']:.0%})\n"
            )
        out.write("\n")

    if stats.get("by_run_type"):
        out.write("By run type:\n")
        for rt, rs in stats["by_run_type"].items():
            out.write(f"  {rt:12s}  {rs['productive']:3d}/{rs['total']:3d}  ({rs['rate']:.0%})\n")
        out.write("\n")

    if stats.get("by_harness"):
        out.write("By harness:\n")
        for harness, hs in stats["by_harness"].items():
            out.write(
                f"  {harness:12s}  {hs['productive']:3d}/{hs['total']:3d}  ({hs['rate']:.0%})\n"
            )
        out.write("\n")

    if stats.get("by_model_run_type"):
        out.write("By model × run type:\n")
        for key, cs in stats["by_model_run_type"].items():
            out.write(f"  {key:25s}  {cs['productive']:3d}/{cs['total']:3d}  ({cs['rate']:.0%})\n")
        out.write("\n")

    if stats.get("by_harness_model"):
        out.write("By harness × model:\n")
        for key, cs in stats["by_harness_model"].items():
            out.write(f"  {key:30s}  {cs['productive']:3d}/{cs['total']:3d}  ({cs['rate']:.0%})\n")


def compute_run_analytics(records: list[SessionRecord]) -> dict:
    """Compute run analytics: duration distribution, NOOP rate, trends."""
    if not records:
        return {"total": 0}

    total = len(records)

    # Duration distribution buckets
    buckets = {"<1m": 0, "1-5m": 0, "5-15m": 0, "15-30m": 0, "30m+": 0, "unknown": 0}
    for r in records:
        d = r.duration_seconds
        if d <= 0:
            buckets["unknown"] += 1
        elif d < 60:
            buckets["<1m"] += 1
        elif d < 300:
            buckets["1-5m"] += 1
        elif d < 900:
            buckets["5-15m"] += 1
        elif d < 1800:
            buckets["15-30m"] += 1
        else:
            buckets["30m+"] += 1

    # NOOP rate by run_type
    noop_by_type: dict[str, dict[str, int]] = {}
    for r in records:
        rt = r.run_type
        if rt not in noop_by_type:
            noop_by_type[rt] = {"total": 0, "noop": 0}
        noop_by_type[rt]["total"] += 1
        if r.outcome == "noop":
            noop_by_type[rt]["noop"] += 1

    # Short run breakdown (<2min with duration data)
    short_runs: dict[str, int] = {}
    for r in records:
        if 0 < r.duration_seconds < 120:
            rt = r.run_type
            short_runs[rt] = short_runs.get(rt, 0) + 1

    # Daily run count
    daily_counts: dict[str, int] = {}
    for r in records:
        try:
            day = r.timestamp[:10]
            daily_counts[day] = daily_counts.get(day, 0) + 1
        except (ValueError, TypeError):
            continue

    # Model × outcome cross-tab
    model_outcome: dict[str, dict[str, int]] = {}
    for r in records:
        if r.model not in model_outcome:
            model_outcome[r.model] = {"total": 0, "productive": 0, "noop": 0}
        model_outcome[r.model]["total"] += 1
        if r.outcome == "productive":
            model_outcome[r.model]["productive"] += 1
        elif r.outcome == "noop":
            model_outcome[r.model]["noop"] += 1

    return {
        "total": total,
        "duration_distribution": buckets,
        "noop_by_run_type": {
            rt: {
                **v,
                "rate": v["noop"] / v["total"] if v["total"] > 0 else 0,
            }
            for rt, v in sorted(noop_by_type.items())
        },
        "short_runs": short_runs,
        "daily_counts": dict(sorted(daily_counts.items())),
        "model_outcome": {
            m: {
                **v,
                "rate": v["productive"] / v["total"] if v["total"] > 0 else 0,
            }
            for m, v in sorted(model_outcome.items())
        },
    }


def format_run_analytics(analytics: dict, out: TextIO = sys.stdout) -> None:
    """Pretty-print run analytics."""
    total = analytics.get("total", 0)
    if total == 0:
        out.write("No session records found.\n")
        return

    out.write(f"Run Analytics ({total} sessions)\n")
    out.write("=" * 50 + "\n\n")

    # Duration distribution
    dist = analytics.get("duration_distribution", {})
    if dist:
        out.write("Duration distribution:\n")
        for bucket, count in dist.items():
            bar = "#" * min(count, 40)
            pct = count / total * 100
            out.write(f"  {bucket:>7s}  {count:3d}  ({pct:4.1f}%)  {bar}\n")
        out.write("\n")

    # NOOP rate by run type
    noop = analytics.get("noop_by_run_type", {})
    if noop:
        out.write("NOOP rate by run type:\n")
        for rt, v in noop.items():
            out.write(f"  {rt:15s}  {v['noop']:3d}/{v['total']:3d}  ({v['rate']:.0%})\n")
        out.write("\n")

    # Short runs
    short = analytics.get("short_runs", {})
    if short:
        out.write("Short runs (<2min) by type:\n")
        for rt, count in sorted(short.items(), key=lambda x: -x[1]):
            out.write(f"  {rt:15s}  {count:3d}\n")
        out.write("\n")

    # Daily counts
    daily = analytics.get("daily_counts", {})
    if daily:
        out.write("Daily run counts:\n")
        for day, count in daily.items():
            bar = "#" * min(count, 40)
            out.write(f"  {day}  {count:3d}  {bar}\n")
        out.write("\n")

    # Model × outcome
    model = analytics.get("model_outcome", {})
    if model:
        out.write("Model × outcome:\n")
        for m, v in model.items():
            out.write(
                f"  {m:12s}  {v['productive']:3d} productive, "
                f"{v['noop']:3d} noop  ({v['rate']:.0%} success)\n"
            )
