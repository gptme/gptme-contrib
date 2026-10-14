"""Shared, opt-in advisory lesson selection protocol and bounded JSON CLI.

Callers retain hard guards, memories and eligibility/holdout policy. Only already
eligible advisory candidates cross this boundary; returned IDs never load files.
"""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_CANDIDATES = 200
MAX_CALLS = 10
MAX_TIMEOUT = 30.0


def _text(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError("expected a string")
    return value


def _integer(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("integer outside protocol bounds")
    return value


def _ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("expected an ID list")
    return list(dict.fromkeys(_text(item) for item in value))


def validate_request(value: Any) -> dict[str, Any]:
    """Validate and normalize v1 input without importing any provider code."""
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise ValueError("missing protocol version")
    if value["version"] != VERSION:
        raise ValueError("unsupported protocol version")
    if len(json.dumps(value, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
        raise ValueError("request too large")
    result: dict[str, Any] = {"version": VERSION}
    for key in ("runtime", "session_id", "context_fingerprint", "policy_version"):
        result[key] = _text(value.get(key))
    result["query"] = _text(value.get("query"), empty=True)
    event = value.get("event_kind")
    if event not in ("session_start", "user_prompt", "tool"):
        raise ValueError("unsupported event kind")
    result["event_kind"] = event
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise ValueError("invalid candidate catalog")
    normalized = []
    seen = set()
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("invalid candidate")
        identifier = _text(item.get("id"))
        if identifier in seen:
            raise ValueError("duplicate candidate ID")
        seen.add(identifier)
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("invalid candidate metadata")
        normalized.append(
            {
                "id": identifier,
                "description": _text(item.get("description"), empty=True),
                "metadata": metadata,
            }
        )
    result["candidates"] = normalized
    result["max_results"] = _integer(value.get("max_results", 3), MAX_CANDIDATES)
    result["fallback_ids"] = [
        identifier
        for identifier in _ids(value.get("fallback_ids"))
        if identifier in seen
    ][: result["max_results"]]
    model = value.get("model")
    result["model"] = None if model is None else _text(model)
    result["max_calls_per_session"] = _integer(
        value.get("max_calls_per_session", 0), MAX_CALLS
    )
    timeout = value.get("timeout_seconds", 2.0)
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_TIMEOUT
    ):
        raise ValueError("invalid deadline")
    result["timeout_seconds"] = float(timeout)
    return result


def fingerprint(request: dict[str, Any]) -> str:
    """Bind cached decisions to intent, complete catalog and selection policy."""
    inputs = {
        key: value
        for key, value in request.items()
        if key not in ("event_kind", "timeout_seconds", "runtime", "session_id")
    }
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def validate_result(value: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Accept only explicit decisions over this request's advisory allowlist."""
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != VERSION
    ):
        raise ValueError("invalid result version")
    status = value.get("status")
    ids = _ids(value.get("ids"))
    allowed = {item["id"] for item in request["candidates"]}
    if any(identifier not in allowed for identifier in ids):
        raise ValueError("unknown selected ID")
    if status not in ("selected", "empty", "fallback"):
        raise ValueError("invalid decision")
    if (status == "empty" and ids) or (status == "selected" and not ids):
        raise ValueError("inconsistent decision")
    if status == "fallback":
        ids = request["fallback_ids"]
    else:
        ids = ids[: request["max_results"]]
        status = "selected" if ids else "empty"
    reason = value.get(
        "reason",
        "fallback" if status == "fallback" else ("selected" if ids else "empty"),
    )
    return {"version": VERSION, "status": status, "ids": ids, "reason": _text(reason)}


def select(
    request: dict[str, Any],
    *,
    state_dir: Path,
    worker_command: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Select with a killable provider process, atomic budget and session cache.

    ``worker_command`` is a trusted embedding/testing seam, never a JSON field.
    Attempts count even on timeout/failure. The caller must use a stable state
    directory and session identity across invocations to preserve that ceiling.
    """
    start = time.monotonic()
    calls = 0
    cache_key = ""
    normalized: dict[str, Any] = {}

    def finish(
        status: str, ids: list[str], reason: str, cached: bool = False
    ) -> dict[str, Any]:
        result = {
            "version": VERSION,
            "status": status,
            "ids": ids,
            "reason": reason,
            "cached": cached,
            "fingerprint": cache_key,
            "calls_used": calls,
            "runtime": normalized.get("runtime", ""),
            "session_id": normalized.get("session_id", ""),
            "event_kind": normalized.get("event_kind", ""),
            "elapsed_ms": round((time.monotonic() - start) * 1000, 3),
        }
        if normalized:
            receipt = {
                **result,
                "record_type": "selection_receipt",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                # One append per invocation; never persist the query/catalog.
                descriptor = os.open(
                    state_dir / "receipts.jsonl",
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                    0o600,
                )
                try:
                    os.write(descriptor, (json.dumps(receipt) + "\n").encode())
                finally:
                    os.close(descriptor)
            except OSError:
                pass  # Receipt failure cannot change an injection decision.
        return result

    def fallback(reason: str) -> dict[str, Any]:
        return finish("fallback", normalized.get("fallback_ids", []), reason)

    try:
        normalized = validate_request(request)
    except (ValueError, TypeError, OverflowError):
        return fallback("invalid_request")
    cache_key = fingerprint(normalized)
    deadline = start + normalized["timeout_seconds"]
    identity = (normalized["runtime"], normalized["session_id"])
    connection = None

    def limit_database_wait() -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        assert connection is not None
        connection.execute(f"PRAGMA busy_timeout={int(min(remaining, 1.0) * 1000)}")

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            state_dir / "selector.sqlite3",
            timeout=min(normalized["timeout_seconds"], 1.0),
        )
        for statement in (
            """CREATE TABLE IF NOT EXISTS sessions (
                runtime TEXT, session_id TEXT, calls INTEGER NOT NULL,
                PRIMARY KEY (runtime, session_id))""",
            """CREATE TABLE IF NOT EXISTS decisions (
                runtime TEXT, session_id TEXT, fingerprint TEXT, result TEXT NOT NULL,
                PRIMARY KEY (runtime, session_id, fingerprint))""",
        ):
            limit_database_wait()
            connection.execute(statement)
        # Reserve before spawning so concurrent prompts cannot overspend.
        limit_database_wait()
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT calls FROM sessions WHERE runtime=? AND session_id=?", identity
        ).fetchone()
        calls = row[0] if row else 0
        row = connection.execute(
            "SELECT result FROM decisions WHERE runtime=? AND session_id=? AND fingerprint=?",
            (*identity, cache_key),
        ).fetchone()
        if row:
            try:
                decision = validate_result(json.loads(row[0]), normalized)
                if decision["status"] == "fallback":
                    raise ValueError("fallback is not a cached decision")
            except (ValueError, TypeError):
                return fallback("invalid_cache")
            return finish(
                decision["status"], decision["ids"], decision["reason"], cached=True
            )
        if normalized["event_kind"] == "tool":
            return fallback("cache_miss")
        if not normalized["candidates"] or not normalized["max_results"]:
            return finish("empty", [], "no_candidates")
        if not normalized["model"]:
            return fallback("missing_model")
        if calls >= normalized["max_calls_per_session"]:
            return fallback("budget_exhausted")
        if time.monotonic() >= deadline:
            return fallback("timeout")
        calls += 1
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(runtime, session_id) DO UPDATE SET calls=excluded.calls",
            (*identity, calls),
        )
        limit_database_wait()
        connection.commit()
        command = (
            list(worker_command)
            if worker_command is not None
            else [sys.executable, "-m", "gptme_lessons_extras._selector_worker"]
        )
        try:
            # run() kills and waits for its child on TimeoutExpired; no orphaned
            # provider thread continues after the caller falls back.
            process = subprocess.run(
                command,
                input=json.dumps(normalized).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=max(0.001, deadline - time.monotonic()),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return fallback("timeout")
        except OSError:
            return fallback("worker_unavailable")
        if process.returncode:
            return fallback("worker_failed")
        try:
            decision = validate_result(json.loads(process.stdout), normalized)
        except (ValueError, TypeError):
            return fallback("invalid_response")
        if decision["status"] == "fallback":
            return fallback(decision["reason"])
        limit_database_wait()
        connection.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?, ?, ?, ?)",
            (*identity, cache_key, json.dumps(decision)),
        )
        limit_database_wait()
        connection.commit()
        return finish(decision["status"], decision["ids"], decision["reason"])
    except TimeoutError:
        return fallback("timeout")
    except (OSError, sqlite3.Error):
        return fallback("state_unavailable")
    finally:
        if connection is not None:
            connection.close()


def main() -> None:
    """Read one bounded JSON request; emit one JSON decision/receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context-json", default="-", help="request file or - for stdin"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
        )
        / "gptme/lesson-selector",
    )
    args = parser.parse_args()
    try:
        if args.context_json == "-":
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        else:
            with Path(args.context_json).open("rb") as stream:
                raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        request = json.loads(raw)
    except (OSError, ValueError):
        request = {}
    print(json.dumps(select(request, state_dir=args.state_dir), allow_nan=False))


if __name__ == "__main__":
    main()
