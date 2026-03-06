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
            If None, generates into a temporary ``_site`` directory.

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
        site_dir = Path("_site")
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
        config = read_workspace_config(ws)
        return jsonify(
            {
                "mode": "dynamic",
                "agent": config.get("agent_name", ws.name),
                "workspace": ws.name,
            }
        )

    @app.route("/api/sessions/stats")
    def api_session_stats() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            from gptme_sessions.store import SessionStore

            store = SessionStore(sessions_dir=ws / "state" / "sessions")

            days = request.args.get("days", type=int)
            if days:
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
            limit = min(limit, 200)  # Cap at 200

            records = store.load_all()
            # Most recent first
            records = sorted(records, key=lambda r: r.timestamp, reverse=True)[:limit]
            return jsonify([r.to_dict() for r in records])
        except ImportError:
            return jsonify({"error": "gptme-sessions not installed"}), 501
        except Exception as e:
            logger.exception("Error loading sessions")
            return jsonify({"error": str(e)}), 500

    return app
