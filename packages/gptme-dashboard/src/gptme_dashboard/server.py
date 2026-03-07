"""Dynamic dashboard server with API endpoints.

Serves the static dashboard HTML plus live API endpoints for
session stats, recent sessions, and agent status. Designed for
progressive enhancement — the static site works without the server,
and dynamic panels activate when the API is reachable.
"""

from __future__ import annotations

import logging
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

    from .generate import generate, read_workspace_config

    # Generate static site if needed
    if site_dir is None:
        site_dir = workspace / "_site"
    if not (site_dir / "index.html").exists():
        generate(workspace, site_dir)

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

    @app.route("/api/sessions/stats")
    def api_session_stats() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            from gptme_sessions.store import SessionStore

            store = SessionStore(sessions_dir=ws / "state" / "sessions")

            days = request.args.get("days", type=int)
            if days is not None and days > 0:
                records = store.query(since_days=days)
            else:
                records = store.load_all()

            stats = store.stats(records)
            return jsonify(stats)
        except ImportError:
            return jsonify({"error": "gptme-sessions not installed"}), 501
        except Exception as e:
            logger.exception("Error computing session stats")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/sessions")
    def api_sessions() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            from gptme_sessions.store import SessionStore

            store = SessionStore(sessions_dir=ws / "state" / "sessions")

            limit = request.args.get("limit", 50, type=int)
            limit = max(1, min(limit, 200))  # Clamp to [1, 200]

            days = request.args.get("days", type=int)
            if days is not None and days > 0:
                records = store.query(since_days=days)
            else:
                records = store.load_all()
            # Most recent first
            records = sorted(records, key=lambda r: r.timestamp, reverse=True)[:limit]
            return jsonify([r.to_dict() for r in records])
        except ImportError:
            return jsonify({"error": "gptme-sessions not installed"}), 501
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

    return app
