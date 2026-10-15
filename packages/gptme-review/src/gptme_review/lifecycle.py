"""Append-only, best-effort AI review lifecycle ledger (separate from measurements)."""

from __future__ import annotations

import fcntl
import json
import math
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

DEFAULT_LEDGER = Path(__file__).resolve().parents[2] / "state/ai-review-lifecycle.jsonl"
HEARTBEAT_INTERVAL_S = 30.0
# Only resolved routing/limit metadata, never arbitrary CLI/environment input.
SAFE_CONFIG_FIELDS = frozenset(
    {
        "repo",
        "number",
        "engine",
        "model",
        "mode",
        "panel_mode",
        "passes",
        "min_agreement",
        "requested_passes",
        "effective_passes",
        "requested_min_agreement",
        "effective_min_agreement",
        "timeout_s",
        "max_tokens",
        "reviewers",
        "families",
        "dry_run",
        "until_dry",
        "max_waves",
        "shard_count",
        "shard_max_chars",
        "structured",
        "requested_engine",
        "requested_model",
        "pr",
        "request_timeout_s",
        "run_timeout_s",
        "shards",
        "fanout",
    }
)


def _safe_config(config: dict) -> dict:
    safe: dict[str, object] = {}
    for key, value in config.items():
        if key not in SAFE_CONFIG_FIELDS:
            continue
        if (
            isinstance(value, str | bool | int)
            or value is None
            or isinstance(value, float)
            and math.isfinite(value)
        ):
            safe[key] = value
        elif isinstance(value, list | tuple) and all(isinstance(v, str) for v in value):
            safe[key] = list(value)
    return safe


TERMINAL_STATUSES = frozenset({"completed", "failed", "timed_out"})
_CURRENT_RUN: ContextVar[RunLifecycle | None] = ContextVar(
    "ai_review_run", default=None
)


def current_run() -> RunLifecycle | None:
    return _CURRENT_RUN.get()


def stage(name: str, **coordinates) -> None:
    """Update the contextual run, if present. Use copy_context for worker threads."""
    run = current_run()
    if run is not None:
        run.stage(name, **coordinates)


def _is_timeout(exc: BaseException) -> bool:
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, TimeoutError | subprocess.TimeoutExpired) or any(
            cls.__name__ == "ReviewDeadlineExceeded" for cls in type(exc).__mro__
        ):
            return True
        cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        if cause is None:
            break
        exc = cause
    return False


@contextmanager
def lifecycle_context():
    """Scope the current run; explicit finish wins over automatic completion."""
    run = RunLifecycle()
    token = _CURRENT_RUN.set(run)
    try:
        yield run
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            code = (
                exc.code
                if isinstance(exc.code, int)
                else (0 if exc.code is None else 1)
            )
            run.finish("completed" if code == 0 else "failed", code, type(exc).__name__)
        else:
            status = "timed_out" if _is_timeout(exc) else "failed"
            run.finish(status, error_type=type(exc).__name__)
        raise
    else:
        run.finish("completed")
    finally:
        _CURRENT_RUN.reset(token)


class RunLifecycle:
    """Start immediately; call finish exactly once to stop the daemon heartbeat."""

    def __init__(self) -> None:
        self.run_id = str(uuid4())
        self._path = Path(
            os.environ.get("AI_REVIEW_LIFECYCLE_LEDGER", str(DEFAULT_LEDGER))
        )
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._finished = False
        self._outcome: tuple[str, str | None] | None = None
        self._stage = "started"
        self._coordinates: dict = {}
        self._context_coordinates: ContextVar[dict] = ContextVar(
            f"review-coordinates-{self.run_id}",
            default={},  # noqa: B039 — default is always copied via dict() at call site
        )
        self._config: dict = {}
        self._deadline: float | None = None
        self._pid = os.getpid()
        self._host = socket.gethostname()
        self._emit("started")
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat, name=f"review-heartbeat-{self.run_id}", daemon=True
        )
        self._heartbeat_thread.start()

    def _emit(self, event: str, **fields) -> None:
        row = {
            "schema_version": 1,
            "record_type": "review_lifecycle",
            "run_id": self.run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "stage": self._stage,
            "pid": self._pid,
            "host": self._host,
            "config": self._config,
            **self._coordinates,
            **fields,
        }
        if self._deadline is not None:
            row["remaining_deadline_s"] = max(0.0, self._deadline - time.monotonic())
        try:
            encoded = json.dumps(row, allow_nan=False) + "\n"
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                stream.write(encoded)
                stream.flush()
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (OSError, TypeError, ValueError):
            # Telemetry must never break the review or leak its content to stderr.
            pass

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            with self._lock:
                if self._finished:
                    return
                self._emit("heartbeat")

    def configure(self, config: dict, timeout_s: float | None = None) -> None:
        """Record safe resolved metadata and establish the monotonic deadline."""
        with self._lock:
            if self._finished:
                return
            self._config.update(_safe_config(config))
            if timeout_s is not None and math.isfinite(timeout_s):
                self._deadline = time.monotonic() + max(0.0, timeout_s)
            self.stage("configured")

    def stage(self, name: str, **coordinates) -> None:
        with self._lock:
            if self._finished:
                return
            self._stage = name
            updated = dict(self._context_coordinates.get())
            for key in ("wave", "shard", "attempt"):
                if key not in coordinates:
                    continue
                # Entering a new outer coordinate discards its old inner coordinates.
                if key == "wave":
                    updated.pop("shard", None)
                    updated.pop("attempt", None)
                elif key == "shard":
                    updated.pop("attempt", None)
                value = coordinates[key]
                if isinstance(value, int) and not isinstance(value, bool):
                    updated[key] = value
                else:
                    updated.pop(key, None)
            self._context_coordinates.set(updated)
            self._coordinates = updated
            self._emit("stage")

    def set_outcome(self, status: str, error_type: str | None = None) -> None:
        """Remember an outcome without ending liveness during final cleanup."""
        if status not in TERMINAL_STATUSES:
            raise ValueError("invalid lifecycle terminal status")
        with self._lock:
            if not self._finished and self._outcome is None:
                self._outcome = (status, error_type)

    def finish(
        self, status: str, exit_code: int | None = None, error_type: str | None = None
    ) -> bool:
        """Write the first terminal only; return whether this call finished the run."""
        if status not in TERMINAL_STATUSES:
            raise ValueError("invalid lifecycle terminal status")
        with self._lock:
            if self._finished:
                return False
            self._finished = True
            self._stop.set()
            if self._outcome is not None:
                status, pending_error = self._outcome
                error_type = pending_error or error_type
            fields: dict = {"status": status}
            if exit_code is not None:
                fields["exit_code"] = exit_code
            if error_type is not None:
                fields["error_type"] = error_type
            self._emit("terminal", **fields)
        self._heartbeat_thread.join()
        return True


def classify_runs(rows, now: datetime, stale_after_s: float = 120) -> dict[str, dict]:
    """Classify parsed ledger rows by run ID; first terminal in ledger order wins.

    Fresh heartbeats prove liveness, not progress. Age alone only establishes
    abandonment, never a timeout. Timestamps for nonterminal events may arrive
    out of order; use the newest event and newest nonheartbeat independently.
    """
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    runs: dict[str, dict] = {}
    latest: dict[str, datetime] = {}
    progress: dict[str, datetime] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        run_id, event = row.get("run_id"), row.get("event")
        if (
            row.get("schema_version") != 1
            or row.get("record_type") != "review_lifecycle"
            or not isinstance(run_id, str)
            or not run_id
            or not isinstance(event, str)
            or event not in {"started", "stage", "heartbeat", "terminal"}
            or not isinstance(row.get("stage"), str)
            or (
                event == "terminal"
                and (
                    not isinstance(row.get("status"), str)
                    or row["status"] not in TERMINAL_STATUSES
                )
            )
        ):
            continue
        try:
            ts = datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        summary = runs.setdefault(run_id, {"run_id": run_id, "terminal": None})
        if summary["terminal"] is not None:
            continue
        if run_id not in latest or ts >= latest[run_id]:
            latest[run_id] = ts
            summary["last_event_ts"] = row["ts"]
        if event != "heartbeat" and (run_id not in progress or ts >= progress[run_id]):
            progress[run_id] = ts
            summary["last_progress_ts"] = row["ts"]
            summary["stage"] = row["stage"]
            summary["config"] = row.get("config", {})
            for key in ("attempt", "shard", "wave"):
                summary.pop(key, None)
                if isinstance(row.get(key), int):
                    summary[key] = row[key]
        if event == "terminal":
            summary["terminal"] = dict(row)
            summary["status"] = row["status"]
    for run_id, summary in runs.items():
        if summary["terminal"] is None:
            summary["status"] = (
                "active"
                if (now - latest[run_id]).total_seconds() <= stale_after_s
                else "abandoned"
            )
        summary.setdefault("stage", None)
        summary.setdefault("last_progress_ts", None)
    return runs


def main() -> int:
    """Operator readout: one JSON summary per run; does not write the ledger."""
    import argparse
    import sys

    source = Path(__file__).resolve().parents[2] / "gptme-contrib/packages/bobutils/src"
    if source.is_dir() and str(source) not in sys.path:
        sys.path.append(str(source))
    from bobutils.jsonl import iter_jsonl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path(os.environ.get("AI_REVIEW_LIFECYCLE_LEDGER", str(DEFAULT_LEDGER))),
    )
    parser.add_argument("--stale-after", type=float, default=120)
    args = parser.parse_args()

    for summary in classify_runs(
        iter_jsonl(args.ledger), datetime.now(timezone.utc), args.stale_after
    ).values():
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
