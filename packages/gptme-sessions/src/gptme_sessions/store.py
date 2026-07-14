"""SessionStore — append-only JSONL persistence for session records."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .record import SessionRecord

try:
    import fcntl as _fcntl

    _has_fcntl = True
except ImportError:  # pragma: no cover - non-POSIX platforms
    _has_fcntl = False

logger = logging.getLogger(__name__)


def _default_sessions_dir() -> Path:
    """Return the default sessions directory (XDG-compliant).

    Checks ``GPTME_SESSIONS_DIR`` env var first, then falls back to
    ``~/.local/share/gptme-sessions/``.
    """
    env_dir = os.environ.get("GPTME_SESSIONS_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".local" / "share" / "gptme-sessions"


class SessionStore:
    """Append-only JSONL store for session records.

    Args:
        sessions_dir: Directory containing the sessions file.
            Defaults to ``~/.local/share/gptme-sessions/`` (or ``GPTME_SESSIONS_DIR``).
        sessions_file: Name of the JSONL file within sessions_dir.
    """

    def __init__(
        self,
        sessions_dir: Path | None = None,
        sessions_file: str = "session-records.jsonl",
    ):
        if sessions_dir is None:
            sessions_dir = _default_sessions_dir()
        self.sessions_dir = sessions_dir
        self.sessions_file = sessions_file
        self.path = sessions_dir / sessions_file
        self._lock_depth = 0

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Cross-process exclusive lock over store mutations.

        ``append()`` and ``rewrite()`` acquire this internally, so any two
        processes mutating the store are serialised.  Callers doing a
        read-modify-write cycle (``load_all()`` → mutate → ``rewrite()``)
        should additionally hold this around the *whole* cycle, or a
        concurrent writer can land between the load and the rewrite and have
        its field updates clobbered by the stale in-memory copy.

        Reentrant within a single ``SessionStore`` instance (flock treats
        separate fds in one process as independent owners, so a naive nested
        acquire would self-deadlock).  Not thread-safe — the store is designed
        for single-threaded CLI processes.

        The lock file is a permanent sentinel next to the store file — never
        delete it.  Deleting it would let a newly arriving process acquire
        LOCK_EX on a fresh inode while a blocked waiter holds LOCK_EX on the
        old inode, breaking mutual exclusion.
        """
        if self._lock_depth > 0:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with open(lock_path, "a", encoding="utf-8") as lock_file:
            if _has_fcntl:
                _fcntl.flock(lock_file, _fcntl.LOCK_EX)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                if _has_fcntl:
                    _fcntl.flock(lock_file, _fcntl.LOCK_UN)

    def _repair_tail(self) -> bool:
        """Remove a corrupt partial JSON line from the end of the file.

        A process killed mid-write (OOM, timeout, SIGKILL) can leave a
        partial JSON record as the last line.  This method detects and
        truncates it so the next ``append()`` starts from a clean state.

        Callers must hold ``lock()`` — a concurrent writer between the read
        and the ftruncate would make the truncation destructive.

        Returns True if a partial line was detected and removed.
        """
        if not self.path.exists():
            return False

        content = self.path.read_bytes()
        if not content:
            return False

        # File ends with a newline — clean termination, no partial line
        if content.endswith(b"\n"):
            return False

        # Find the last complete line boundary
        last_newline = content.rfind(b"\n")

        if last_newline == -1:
            # Single line, no newline at all.  If it is valid JSON the file
            # was never corrupted; just missing a trailing newline.
            # If not valid, the damage is unrecoverable — leave it alone
            # rather than silently deleting data.
            try:
                json.loads(content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Corrupt single-line file %s — not truncating", self.path)
            return False

        partial = content[last_newline + 1 :]
        try:
            json.loads(partial.decode("utf-8"))
            return False  # valid JSON even without trailing newline
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        # Truncate to the last valid line (including its newline).
        # Use ftruncate (f.truncate) rather than write_bytes so that a process
        # kill between the zero-truncate and the rewrite cannot destroy all
        # prior records.  ftruncate is a single syscall that only shortens the
        # file; it never zeros it first.
        keep_bytes = last_newline + 1
        with open(self.path, "r+b") as f:
            f.truncate(keep_bytes)
            f.flush()
            os.fsync(f.fileno())
        logger.warning(
            "Repaired corrupt tail in %s: removed %d bytes",
            self.path,
            len(content) - keep_bytes,
        )
        return True

    def append(self, record: SessionRecord) -> Path:
        """Append a session record to the JSONL store.  Self-heals corrupt tails."""
        with self.lock():
            self._repair_tail()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
                f.flush()
                os.fsync(f.fileno())
        return self.path

    def load_all(self) -> list[SessionRecord]:
        """Load all session records from the JSONL store."""
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(SessionRecord.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        continue
        return records

    def rewrite(self, records: list[SessionRecord]) -> Path:
        """Atomically rewrite the JSONL store with updated records.

        **Additive (upsert) semantics**: ``records`` is treated as an upsert
        set, *not* a full replacement.  Any record present in the on-disk file
        but absent from ``records`` is silently preserved (re-read from disk
        and appended to the output).  There is no way to delete a record via
        this method; to remove records, edit the JSONL file directly.

        Holds the store lock across the re-read → write → replace cycle, so
        concurrent ``append()``/``rewrite()`` calls are serialised: an append
        either lands before the re-read (and is preserved) or blocks until
        the replace completes (and lands in the new file).  The re-read also:
        - Preserves malformed JSONL lines rather than silently dropping them.
        - Picks up records appended between the caller's ``load_all()`` and
          this call.  Callers that *mutate* loaded records should hold
          ``lock()`` around their whole load → mutate → rewrite cycle to
          avoid clobbering a concurrent writer's field updates with a stale
          in-memory copy.

        The temp file is per-pid and fsynced before the atomic replace: a
        fixed shared temp name would let two concurrent rewriters interleave
        writes on the same inode and install a torn file (the mechanism
        behind truncated mid-file records observed 2026-07-14).

        ``records`` takes precedence for any session_id present in both.
        """
        with self.lock():
            known_ids = {r.session_id for r in records}
            extra_records: list[SessionRecord] = []
            malformed_lines: list[str] = []

            if self.path.exists():
                with open(self.path, encoding="utf-8") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = SessionRecord.from_dict(json.loads(raw))
                            if rec.session_id not in known_ids:
                                extra_records.append(rec)
                        except (json.JSONDecodeError, TypeError, AttributeError):
                            malformed_lines.append(raw)

            tmp_path = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for record in records:
                        f.write(record.to_json() + "\n")
                    for record in extra_records:
                        f.write(record.to_json() + "\n")
                    for line in malformed_lines:
                        f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                tmp_path.replace(self.path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        return self.path

    def query(
        self,
        model: str | None = None,
        run_type: str | None = None,
        category: str | None = None,
        harness: str | None = None,
        outcome: str | None = None,
        since_days: float | None = None,
        project: str | None = None,
    ) -> list[SessionRecord]:
        """Filter session records by criteria."""
        records = self.load_all()
        if model:
            records = [r for r in records if r.model_normalized == model or r.model == model]
        if run_type:
            records = [r for r in records if r.run_type == run_type]
        if category:
            records = [r for r in records if r.category == category]
        if harness:
            records = [r for r in records if r.harness == harness]
        if outcome:
            records = [r for r in records if r.outcome == outcome]
        if project:
            records = [r for r in records if r.project and project in r.project]
        if since_days is not None:
            cutoff = datetime.now(timezone.utc).timestamp() - (since_days * 86400)
            filtered = []
            for r in records:
                try:
                    ts = datetime.fromisoformat(r.timestamp.replace("Z", "+00:00")).timestamp()
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
        violated_policy = sum(1 for r in records if r.outcome == "violated_policy")

        # Model breakdown (uses normalized names for grouping)
        model_stats: dict[str, dict[str, int]] = {}
        for r in records:
            m = r.model_normalized or "null"
            if m not in model_stats:
                model_stats[m] = {"total": 0, "productive": 0}
            model_stats[m]["total"] += 1
            if r.outcome == "productive":
                model_stats[m]["productive"] += 1

        # Run-type breakdown
        run_type_stats: dict[str, dict[str, int]] = {}
        for r in records:
            rt = r.run_type or "null"
            if rt not in run_type_stats:
                run_type_stats[rt] = {"total": 0, "productive": 0}
            run_type_stats[rt]["total"] += 1
            if r.outcome == "productive":
                run_type_stats[rt]["productive"] += 1

        # Model × run-type cross-tab (normalized model for grouping)
        cross_tab: dict[str, dict[str, int]] = {}
        for r in records:
            key = f"{r.model_normalized or 'null'}×{r.run_type or 'null'}"
            if key not in cross_tab:
                cross_tab[key] = {"total": 0, "productive": 0}
            cross_tab[key]["total"] += 1
            if r.outcome == "productive":
                cross_tab[key]["productive"] += 1

        # Harness breakdown
        harness_stats: dict[str, dict[str, int]] = {}
        for r in records:
            h = r.harness or "null"
            if h not in harness_stats:
                harness_stats[h] = {"total": 0, "productive": 0}
            harness_stats[h]["total"] += 1
            if r.outcome == "productive":
                harness_stats[h]["productive"] += 1

        # Harness × model cross-tab (normalized model for grouping)
        harness_model_tab: dict[str, dict[str, int]] = {}
        for r in records:
            key = f"{r.harness or 'null'}×{r.model_normalized or 'null'}"
            if key not in harness_model_tab:
                harness_model_tab[key] = {"total": 0, "productive": 0}
            harness_model_tab[key]["total"] += 1
            if r.outcome == "productive":
                harness_model_tab[key]["productive"] += 1

        # Project breakdown
        project_stats: dict[str, dict[str, int]] = {}
        for r in records:
            proj = r.project
            if not proj:
                continue
            # Use last path component for display
            proj_short = proj.rstrip("/").rsplit("/", 1)[-1] if "/" in proj else proj
            if proj_short not in project_stats:
                project_stats[proj_short] = {"total": 0, "productive": 0}
            project_stats[proj_short]["total"] += 1
            if r.outcome == "productive":
                project_stats[proj_short]["productive"] += 1

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

        result: dict = {
            "total": total,
            "productive": productive,
            "noop": noop,
            "violated_policy": violated_policy,
            "success_rate": productive / total if total > 0 else 0,
            "duration": duration_stats,
            "by_model": {m: _rate(s) for m, s in sorted(model_stats.items())},
            "by_run_type": {rt: _rate(s) for rt, s in sorted(run_type_stats.items())},
            "by_model_run_type": {k: _rate(s) for k, s in sorted(cross_tab.items())},
            "by_harness": {h: _rate(s) for h, s in sorted(harness_stats.items())},
            "by_harness_model": {k: _rate(s) for k, s in sorted(harness_model_tab.items())},
        }
        if project_stats:
            result["by_project"] = {p: _rate(s) for p, s in sorted(project_stats.items())}
        return result


def format_stats(stats: dict, out: TextIO = sys.stdout) -> None:
    """Pretty-print session statistics."""
    total = stats.get("total", 0)
    if total == 0:
        out.write("No session records found.\n")
        return

    rate = stats.get("success_rate", 0)
    violated = stats.get("violated_policy", 0)
    out.write(f"Sessions: {total} total, {stats['productive']} productive ")
    out.write(f"({rate:.0%} success rate)\n")
    if violated:
        out.write(f"  Policy violations: {violated} session(s)\n")

    dur = stats.get("duration", {})
    if dur:
        avg_min = dur["avg"] / 60
        total_hrs = dur["total_hours"]
        out.write(
            f"Duration: {dur['count']} with data, avg {avg_min:.0f}m, total {total_hrs:.1f}h\n"
        )
    out.write("\n")

    # Dynamically compute column width for model names
    model_width = 12
    if stats.get("by_model"):
        model_width = max(model_width, max(len(m) for m in stats["by_model"]))

    if stats.get("by_model"):
        out.write("By model:\n")
        for model, ms in stats["by_model"].items():
            out.write(
                f"  {model:<{model_width}}  "
                f"{ms['productive']:3d}/{ms['total']:3d}  ({ms['rate']:.0%})\n"
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

    if stats.get("by_project"):
        out.write("By project:\n")
        proj_width = max(12, max(len(p) for p in stats["by_project"]))
        for proj, ps in stats["by_project"].items():
            out.write(
                f"  {proj:<{proj_width}}  "
                f"{ps['productive']:3d}/{ps['total']:3d}  ({ps['rate']:.0%})\n"
            )
        out.write("\n")

    # Only show cross-tabs when both dimensions have 2+ distinct values
    if stats.get("by_model_run_type"):
        models = {k.split("×")[0] for k in stats["by_model_run_type"]}
        run_types = {k.split("×")[1] for k in stats["by_model_run_type"]}
        if len(models) >= 2 and len(run_types) >= 2:
            cross_width = max(25, max(len(k) for k in stats["by_model_run_type"]))
            out.write("By model × run type:\n")
            for key, cs in stats["by_model_run_type"].items():
                out.write(
                    f"  {key:<{cross_width}}  "
                    f"{cs['productive']:3d}/{cs['total']:3d}  ({cs['rate']:.0%})\n"
                )
            out.write("\n")

    if stats.get("by_harness_model"):
        harnesses = {k.split("×")[0] for k in stats["by_harness_model"]}
        models_hm = {k.split("×")[1] for k in stats["by_harness_model"]}
        if len(harnesses) >= 2 and len(models_hm) >= 2:
            hm_width = max(30, max(len(k) for k in stats["by_harness_model"]))
            out.write("By harness × model:\n")
            for key, cs in stats["by_harness_model"].items():
                out.write(
                    f"  {key:<{hm_width}}  "
                    f"{cs['productive']:3d}/{cs['total']:3d}  ({cs['rate']:.0%})\n"
                )
            out.write("\n")


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
        rt = r.run_type or "null"
        if rt not in noop_by_type:
            noop_by_type[rt] = {"total": 0, "noop": 0}
        noop_by_type[rt]["total"] += 1
        if r.outcome == "noop":
            noop_by_type[rt]["noop"] += 1

    # Short run breakdown (<2min with duration data)
    short_runs: dict[str, int] = {}
    for r in records:
        if 0 < r.duration_seconds < 120:
            rt = r.run_type or "null"
            short_runs[rt] = short_runs.get(rt, 0) + 1

    # Daily run count
    daily_counts: dict[str, int] = {}
    for r in records:
        try:
            day = r.timestamp[:10]
            daily_counts[day] = daily_counts.get(day, 0) + 1
        except (ValueError, TypeError):
            continue

    # Model × outcome cross-tab (normalized model for grouping)
    model_outcome: dict[str, dict[str, int]] = {}
    for r in records:
        m = r.model_normalized or "null"
        if m not in model_outcome:
            model_outcome[m] = {"total": 0, "productive": 0, "noop": 0}
        model_outcome[m]["total"] += 1
        if r.outcome == "productive":
            model_outcome[m]["productive"] += 1
        elif r.outcome == "noop":
            model_outcome[m]["noop"] += 1

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
