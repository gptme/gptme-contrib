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

    # Generate static site if needed
    if site_dir is None:
        site_dir = workspace / "_site"
    if not (site_dir / "index.html").exists():
        # Don't bake sessions into the static HTML in serve mode — the live
        # /api/sessions endpoint provides fresh session data.  Baking them in
        # would create a duplicate sessions panel (static + dynamic).
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

    @app.route("/api/sessions")
    def api_sessions() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            limit = request.args.get("limit", 50, type=int)
            limit = max(1, min(limit, 200))
            days = request.args.get("days", type=int)

            # Try SessionStore first
            store_result = _load_sessions_from_store(ws, days)
            if store_result is not None:
                records, _store = store_result
                records = sorted(records, key=lambda r: r.timestamp, reverse=True)[:limit]
                return jsonify([r.to_dict() for r in records])

            # Fallback: scan actual session logs
            scanned = _get_scanned_sessions(ws, days if days and days > 0 else 30)[:limit]
            result = []
            for s in scanned:
                result.append(
                    {
                        "timestamp": s.get("date", ""),
                        "harness": s.get("harness", ""),
                        "model": s.get("model", ""),
                        "category": s.get("category", ""),
                        "outcome": "productive" if s.get("grade", 0) >= 0.4 else "noop",
                        "duration_seconds": 0,
                    }
                )
            return jsonify(result)
        except Exception as e:
            logger.exception("Error loading sessions")
            return jsonify({"error": str(e)}), 500

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
            try:
                config = read_workspace_config(ws)
                agent_name = config.get("agent_name", "").lower()
            except Exception:
                agent_name = ""

            def _is_relevant(name: str) -> bool:
                n = name.lower()
                return "gptme" in n or (bool(agent_name) and agent_name in n)

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
                            if _is_relevant(name):
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
                            if _is_relevant(label):
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
