"""Dynamic dashboard server with API endpoints.

Serves the static dashboard HTML plus live API endpoints for
session stats, recent sessions, and agent status. Designed for
progressive enhancement — the static site works without the server,
and dynamic panels activate when the API is reachable.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def create_app(workspace: Path, site_dir: Path | None = None) -> Any:
    """Create Flask app serving static dashboard + API.

    Args:
        workspace: Path to the gptme workspace root.
        site_dir: Directory containing the generated static site.
            If None, generates into ``<workspace>/_site``.

    Returns:
        A Flask application instance.

    Raises:
        ImportError: If Flask is not installed.
    """
    try:
        from flask import Flask, jsonify, request
    except ImportError:
        raise ImportError(
            "Flask is required for 'gptme-dashboard serve'. "
            "Install with: pip install gptme-dashboard[serve]"
        )

    from . import generate as _gen_mod
    from .generate import generate, read_workspace_config, scan_journals, scan_summaries, scan_tasks

    # Generate (or refresh) static site on every serve start.
    # Regenerating ensures the site reflects the current workspace state —
    # journals, tasks, lessons added since the last ``generate`` run are
    # immediately visible.  Sessions are excluded so the live /api/sessions
    # endpoint provides fresh data without a duplicate static panel.
    if site_dir is None:
        site_dir = workspace / "_site"
    generate(workspace, site_dir, include_sessions=False)

    app = Flask(
        __name__,
        static_folder=str(site_dir.resolve()),
        static_url_path="",
    )
    app.config["WORKSPACE"] = str(workspace.resolve())

    @app.route("/")
    def index() -> Any:
        return app.send_static_file("index.html")

    @app.route("/api/status")
    def api_status() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            config = read_workspace_config(ws)
            return jsonify(
                {
                    "mode": "dynamic",
                    "agent": config.get("agent_name", ws.name),
                    "workspace": ws.name,
                }
            )
        except Exception as e:
            logger.exception("Error reading workspace config")
            return jsonify({"error": str(e)}), 500

    def _load_sessions_from_store(
        ws: Path, days: int | None = None
    ) -> tuple[list[Any], Any] | None:
        """Try loading sessions from SessionStore (structured JSONL records).

        Returns (records, store) if data is available, or None to signal the
        caller to fall back to scan_recent_sessions.

        ``days=None`` means no time filter (load all records).
        ``days=0`` is treated as the default window (30 days) to stay consistent
        with the scan_recent_sessions fallback path.
        ``days>0`` filters to the last N days.
        """
        try:
            from gptme_sessions.store import SessionStore

            store = SessionStore(sessions_dir=ws / "state" / "sessions")
            if days and days > 0:
                records = store.query(since_days=days)
            elif days is None:
                records = store.load_all()
            else:
                # days=0: use default window to match scan fallback behaviour
                records = store.query(since_days=30)
            if not records:
                return None
            return list(records), store
        except Exception:
            return None

    # Cache for scan_recent_sessions (expensive); expires after 5 minutes
    _SCAN_CACHE_TTL = 300
    _scan_cache: dict[str, Any] = {"data": None, "days": None, "expires": 0.0}

    def _get_scanned_sessions(ws: Path, days: int = 30) -> list[dict[str, Any]]:
        """Fallback: discover sessions from gptme/CC log directories."""
        now = time.monotonic()
        if (
            _scan_cache["data"] is None
            or _scan_cache["days"] != days
            or now >= _scan_cache["expires"]
        ):
            _scan_cache["data"] = list(_gen_mod.scan_recent_sessions(ws, days=days))
            _scan_cache["days"] = days
            _scan_cache["expires"] = now + _SCAN_CACHE_TTL
        return _scan_cache["data"]  # type: ignore[no-any-return]

    @app.route("/api/sessions/stats")
    def api_session_stats() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            days = request.args.get("days", type=int)

            # Try SessionStore first (fast, structured)
            store_result = _load_sessions_from_store(ws, days)
            if store_result is not None:
                records, store = store_result
                return jsonify(store.stats(records))

            # Fallback: scan actual session logs
            scanned = _get_scanned_sessions(ws, days if days and days > 0 else 30)
            if not scanned:
                return jsonify({"total": 0})

            total = len(scanned)
            productive = sum(1 for s in scanned if s.get("grade", 0) >= 0.4)
            noop = total - productive
            success_rate = productive / total if total > 0 else 0

            by_model: dict[str, dict] = {}
            by_harness: dict[str, dict] = {}
            for s in scanned:
                for key, bucket in [
                    (s.get("model", "unknown"), by_model),
                    (s.get("harness", "unknown"), by_harness),
                ]:
                    if key not in bucket:
                        bucket[key] = {"total": 0, "productive": 0}
                    bucket[key]["total"] += 1
                    if s.get("grade", 0) >= 0.4:
                        bucket[key]["productive"] += 1
            for bucket in (by_model, by_harness):
                for m in bucket.values():
                    m["rate"] = m["productive"] / m["total"] if m["total"] > 0 else 0

            return jsonify(
                {
                    "total": total,
                    "productive": productive,
                    "noop": noop,
                    "success_rate": success_rate,
                    "by_model": by_model,
                    "by_harness": by_harness,
                }
            )
        except Exception as e:
            logger.exception("Error computing session stats")
            return jsonify({"error": str(e)}), 500

    def _filter_records(
        records: list[dict[str, Any]],
        model: str | None,
        harness: str | None,
        outcome: str | None,
    ) -> list[dict[str, Any]]:
        """Apply model/harness/outcome filters to session dicts."""
        if model:
            records = [r for r in records if r.get("model", "").lower() == model.lower()]
        if harness:
            records = [r for r in records if r.get("harness", "").lower() == harness.lower()]
        if outcome:
            records = [r for r in records if r.get("outcome", "").lower() == outcome.lower()]
        return records

    @app.route("/api/sessions")
    def api_sessions() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            limit = request.args.get("limit", 50, type=int)
            limit = max(1, min(limit, 200))
            offset = request.args.get("offset", 0, type=int)
            offset = max(0, offset)
            days = request.args.get("days", type=int)

            # Filter parameters
            model_filter = request.args.get("model")
            harness_filter = request.args.get("harness")
            outcome_filter = request.args.get("outcome")

            # Try SessionStore first
            store_result = _load_sessions_from_store(ws, days)
            if store_result is not None:
                records, _store = store_result
                records = sorted(records, key=lambda r: r.timestamp, reverse=True)
                # Convert to dicts for uniform filtering
                all_dicts = [r.to_dict() for r in records]
                all_dicts = _filter_records(all_dicts, model_filter, harness_filter, outcome_filter)
                total = len(all_dicts)
                page = all_dicts[offset : offset + limit]
                return jsonify(
                    {
                        "sessions": page,
                        "total": total,
                        "offset": offset,
                        "has_more": offset + limit < total,
                    }
                )

            # Fallback: scan actual session logs
            scanned = _get_scanned_sessions(ws, days if days and days > 0 else 30)
            all_dicts = []
            for s in scanned:
                all_dicts.append(
                    {
                        "timestamp": s.get("date", ""),
                        "harness": s.get("harness", ""),
                        "model": s.get("model", ""),
                        "category": s.get("category", ""),
                        "outcome": "productive" if s.get("grade", 0) >= 0.4 else "noop",
                        "duration_seconds": 0,
                    }
                )
            all_dicts = _filter_records(all_dicts, model_filter, harness_filter, outcome_filter)
            total = len(all_dicts)
            page = all_dicts[offset : offset + limit]
            return jsonify(
                {
                    "sessions": page,
                    "total": total,
                    "offset": offset,
                    "has_more": offset + limit < total,
                }
            )
        except Exception as e:
            logger.exception("Error loading sessions")
            return jsonify({"error": str(e)}), 500

    def _get_agent_name(ws: Path) -> str:
        """Read the agent name from workspace config, lowercased."""
        try:
            config = read_workspace_config(ws)
            name: str = config.get("agent_name", "")
            return name.lower()
        except Exception:
            return ""

    def _is_relevant_service(name: str, agent_name: str) -> bool:
        """Check if a service name is gptme/agent-related."""
        n = name.lower()
        return "gptme" in n or (bool(agent_name) and agent_name in n)

    # Cache for health endpoint (expensive journalctl calls); 60s TTL
    _HEALTH_CACHE_TTL = 60
    _health_cache: dict[str, Any] = {"data": None, "expires": 0.0}

    @app.route("/api/services")
    def api_services() -> Any:
        """Return systemd/launchd service status for gptme-related services.

        Detects services whose names contain "gptme" or the agent name from
        gptme.toml.  Works on Linux (systemd --user) and macOS (launchctl).
        Returns an empty list gracefully when detection is unavailable.
        """
        import json as _json
        import platform
        import subprocess

        ws = Path(app.config["WORKSPACE"])
        try:
            agent_name = _get_agent_name(ws)

            system = platform.system()
            services: list[dict] = []

            if system == "Linux":
                try:
                    result = subprocess.run(
                        [
                            "systemctl",
                            "--user",
                            "list-units",
                            "--all",
                            "--type=service",
                            "--output=json",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        units = _json.loads(result.stdout)
                        for unit in units:
                            name = unit.get("unit", "")
                            if _is_relevant_service(name, agent_name):
                                services.append(
                                    {
                                        "name": name,
                                        "description": unit.get("description", ""),
                                        "active": unit.get("active", ""),
                                        "sub": unit.get("sub", ""),
                                    }
                                )
                except (OSError, subprocess.TimeoutExpired, _json.JSONDecodeError, ValueError):
                    pass

            elif system == "Darwin":
                try:
                    result = subprocess.run(
                        ["launchctl", "list"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        for line in result.stdout.splitlines()[1:]:  # Skip header
                            parts = line.split("\t", 2)
                            if len(parts) < 3:
                                continue
                            pid, _status, label = parts
                            if _is_relevant_service(label, agent_name):
                                running = pid.strip() != "-"
                                services.append(
                                    {
                                        "name": label,
                                        "description": "",
                                        "active": "active" if running else "inactive",
                                        "sub": "running" if running else "dead",
                                    }
                                )
                except (OSError, subprocess.TimeoutExpired):
                    pass

            return jsonify({"services": services, "platform": system})
        except Exception as e:
            logger.exception("Error detecting services")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/schedule")
    def api_schedule() -> Any:
        """Return systemd timer schedule for gptme-related timers.

        Complements ``/api/services`` by showing *when* services run, not just
        whether they are running.  On Linux, parses ``systemctl --user
        list-timers``.  Returns an empty list on macOS (not yet supported),
        when detection is unavailable, or when no relevant timers exist.
        """
        import json as _json
        import platform
        import subprocess
        from datetime import datetime, timezone

        ws = Path(app.config["WORKSPACE"])
        try:
            agent_name = _get_agent_name(ws)

            system = platform.system()
            timers: list[dict] = []

            if system == "Linux":
                try:
                    result = subprocess.run(
                        [
                            "systemctl",
                            "--user",
                            "list-timers",
                            "--all",
                            "--output=json",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        units = _json.loads(result.stdout)
                        for unit in units:
                            timer_name = unit.get("unit", "")
                            if not _is_relevant_service(timer_name, agent_name):
                                continue
                            # Timestamps are in microseconds since epoch
                            next_us = unit.get("next", 0)
                            last_us = unit.get("last", 0)
                            next_iso = (
                                datetime.fromtimestamp(
                                    next_us / 1_000_000, tz=timezone.utc
                                ).isoformat()
                                if next_us > 0
                                else None
                            )
                            last_iso = (
                                datetime.fromtimestamp(
                                    last_us / 1_000_000, tz=timezone.utc
                                ).isoformat()
                                if last_us > 0
                                else None
                            )
                            timers.append(
                                {
                                    "name": timer_name,
                                    "activates": unit.get("activates", ""),
                                    "next": next_iso,
                                    "last": last_iso,
                                }
                            )
                except (
                    OSError,
                    subprocess.TimeoutExpired,
                    _json.JSONDecodeError,
                    ValueError,
                    OverflowError,
                ):
                    pass

            elif system == "Darwin":
                # macOS doesn't have a direct timer-list command;
                # return empty — services endpoint already covers launchd.
                pass

            return jsonify({"timers": timers, "platform": system})
        except Exception as e:
            logger.exception("Error detecting schedule")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/services/health")
    def api_services_health() -> Any:
        """Return detailed health metrics for gptme-related services.

        For each relevant service, returns:
        - status: active/inactive/failed state
        - health: healthy/degraded/unhealthy classification
        - uptime_seconds: time since service started
        - memory_bytes: cgroups-based memory accounting (requires MemoryAccounting=yes in unit)
        - restart_count: NRestarts from systemd
        - recent_errors: count of error/warning lines in last hour
        - pid: main process ID (if running)

        Uses a 60-second cache since journalctl queries are expensive.
        Linux (systemd --user) only; returns empty on other platforms.
        """
        import json as _json
        import platform
        import subprocess
        from datetime import datetime, timezone

        now = time.monotonic()
        if _health_cache["data"] is not None and now < _health_cache["expires"]:
            return jsonify(_health_cache["data"])

        ws = Path(app.config["WORKSPACE"])
        system = platform.system()

        if system != "Linux":
            result: dict[str, Any] = {"services": [], "platform": system}
            _health_cache["data"] = result
            _health_cache["expires"] = now + _HEALTH_CACHE_TTL
            return jsonify(result)

        try:
            agent_name = _get_agent_name(ws)

            # Get list of relevant services
            try:
                list_result = subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "list-units",
                        "--all",
                        "--type=service",
                        "--output=json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if list_result.returncode != 0 or not list_result.stdout.strip():
                    result = {"services": [], "platform": system}
                    _health_cache["data"] = result
                    _health_cache["expires"] = now + _HEALTH_CACHE_TTL
                    return jsonify(result)

                units = _json.loads(list_result.stdout)
                relevant = [u for u in units if _is_relevant_service(u.get("unit", ""), agent_name)]
            except (OSError, subprocess.TimeoutExpired, _json.JSONDecodeError, ValueError):
                result = {"services": [], "platform": system}
                _health_cache["data"] = result
                _health_cache["expires"] = now + _HEALTH_CACHE_TTL
                return jsonify(result)

            health_list: list[dict[str, Any]] = []
            utcnow = datetime.now(timezone.utc)

            for unit in relevant:
                name = unit.get("unit", "")
                active = unit.get("active", "unknown")
                sub = unit.get("sub", "unknown")

                # Get detailed properties via systemctl show
                props: dict[str, str] = {}
                try:
                    show_result = subprocess.run(
                        [
                            "systemctl",
                            "--user",
                            "show",
                            name,
                            "--property=MainPID,ActiveEnterTimestamp,NRestarts,MemoryCurrent",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if show_result.returncode == 0:
                        for line in show_result.stdout.strip().splitlines():
                            if "=" in line:
                                k, v = line.split("=", 1)
                                props[k.strip()] = v.strip()
                except (OSError, subprocess.TimeoutExpired):
                    pass

                # Parse uptime using Python datetime (avoids subprocess per service)
                uptime_seconds = 0
                active_since = props.get("ActiveEnterTimestamp", "")
                if active_since and active_since != "n/a":
                    try:
                        # systemd timestamps: "Mon 2026-03-10 10:00:00 UTC"
                        # Strip the weekday prefix and parse date+time directly
                        parts = active_since.split()
                        if len(parts) >= 3:
                            dt_str = parts[1] + " " + parts[2]
                            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(
                                tzinfo=timezone.utc
                            )
                            uptime_seconds = max(0, int((utcnow - dt).total_seconds()))
                    except (ValueError, IndexError):
                        pass

                # Parse PID
                pid = 0
                try:
                    pid = int(props.get("MainPID", "0"))
                except ValueError:
                    pass

                # Parse memory (MemoryCurrent is in bytes, may be "[not set]")
                memory_bytes = 0
                mem_str = props.get("MemoryCurrent", "")
                if mem_str and mem_str not in ("[not set]", "infinity"):
                    try:
                        memory_bytes = int(mem_str)
                    except (ValueError, OverflowError):
                        pass

                # Parse restart count
                restart_count = 0
                try:
                    restart_count = int(props.get("NRestarts", "0"))
                except ValueError:
                    pass

                # Count recent errors from journal (last 1 hour)
                recent_errors = 0
                try:
                    journal_result = subprocess.run(
                        [
                            "journalctl",
                            "--user",
                            "-u",
                            name,
                            "--since",
                            "1 hour ago",
                            "--priority=warning",
                            "--no-pager",
                            "-q",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if journal_result.returncode == 0:
                        lines = journal_result.stdout.strip().splitlines()
                        recent_errors = len(lines) if lines and lines[0] else 0
                except (OSError, subprocess.TimeoutExpired):
                    pass

                # Classify health
                # Treat transient systemd states (reloading/activating/deactivating)
                # as non-unhealthy — they are normal lifecycle transitions, not failures.
                _active_states = frozenset({"active", "reloading", "activating", "deactivating"})
                if active not in _active_states:
                    health = "unhealthy"
                elif recent_errors > 20 or restart_count > 5:
                    health = "degraded"
                elif recent_errors > 0 or restart_count > 0:
                    health = "warning"
                else:
                    health = "healthy"

                health_list.append(
                    {
                        "name": name,
                        "active": active,
                        "sub": sub,
                        "health": health,
                        "uptime_seconds": uptime_seconds,
                        "memory_bytes": memory_bytes,
                        "restart_count": restart_count,
                        "recent_errors": recent_errors,
                        "pid": pid,
                    }
                )

            result = {"services": health_list, "platform": system}
            _health_cache["data"] = result
            _health_cache["expires"] = now + _HEALTH_CACHE_TTL
            return jsonify(result)
        except Exception as e:
            logger.exception("Error computing service health")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/journals")
    def api_journals() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            limit = request.args.get("limit", 30, type=int)
            limit = max(1, min(limit, 100))
            entries = scan_journals(ws, limit=limit)
            return jsonify(entries)
        except Exception as e:
            logger.exception("Error scanning journals")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tasks")
    def api_tasks() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            tasks = scan_tasks(ws)
            state_filter = request.args.get("state")
            if state_filter:
                tasks = [t for t in tasks if t["state"] == state_filter.lower()]
            limit = request.args.get("limit", 100, type=int)
            limit = max(1, min(limit, 500))
            return jsonify(tasks[:limit])
        except Exception as e:
            logger.exception("Error scanning tasks")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/summaries")
    def api_summaries() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            limit = request.args.get("limit", 20, type=int)
            limit = max(1, min(limit, 100))
            period_type = request.args.get("type", "")
            if period_type and period_type not in ("daily", "weekly", "monthly"):
                return jsonify(
                    {
                        "error": f"Invalid type '{period_type}'. Must be one of: daily, weekly, monthly"
                    }
                ), 400
            entries = scan_summaries(ws, limit=limit, period_type=period_type)
            return jsonify(entries)
        except Exception as e:
            logger.exception("Error scanning summaries")
            return jsonify({"error": str(e)}), 500

    return app
