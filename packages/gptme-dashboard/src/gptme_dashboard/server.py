"""Dynamic dashboard server with API endpoints.

Serves the static dashboard HTML plus live API endpoints for
session stats, recent sessions, and agent status. Designed for
progressive enhancement — the static site works without the server,
and dynamic panels activate when the API is reachable.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import platform
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block all HTTP redirects to prevent SSRF via open redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.URLError(f"redirect not allowed: {newurl}")


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler())


def _fetch_json(url: str, timeout: int = 5) -> "dict[str, Any] | list[Any] | None":
    """Fetch JSON from *url* without following HTTP redirects.

    Returns ``None`` on any error (connection refused, timeout, redirect, …).
    Redirects are blocked to prevent SSRF via a compromised agent API.
    """
    try:
        with _no_redirect_opener.open(url, timeout=timeout) as resp:  # noqa: S310
            result: dict[str, Any] | list[Any] = json.loads(resp.read(64 * 1024))
            return result
    except Exception:
        return None


def load_org_config(org_config: Path) -> list[dict[str, str]]:
    """Load org config from a TOML file listing known agents.

    Expected format::

        [[agents]]
        name = "bob"
        api  = "https://bob.example.com:8042"

        [[agents]]
        name = "alice"
        api  = "https://alice.example.com:8042"

    Returns a list of agent dicts with ``name`` and ``api`` keys.
    Raises ``ValueError`` for invalid entries (missing name/api, bad URL scheme).
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError:
            raise ImportError(
                "tomli is required for org config on Python < 3.11: " "pip install tomli"
            )

    with open(org_config, "rb") as f:
        data = tomllib.load(f)

    agents = data.get("agents", [])
    if not isinstance(agents, list):
        raise ValueError(
            f"'agents' must be an array-of-tables ([[agents]]), got: {type(agents).__name__}"
        )
    result = []
    for i, agent in enumerate(agents):
        if "name" not in agent:
            raise ValueError(f"agents[{i}] missing 'name' field")
        if "api" not in agent:
            raise ValueError(f"agents[{i}] missing 'api' field")
        api = agent["api"].rstrip("/")
        if not api.startswith(("http://", "https://")):
            raise ValueError(f"agents[{i}].api must start with http:// or https://, got: {api!r}")
        result.append({"name": agent["name"], "api": api})
    return result


def _fetch_agent_card(agent: "dict[str, str]") -> "dict[str, Any]":
    """Fetch all data for a single agent — status then tasks/services/sessions in parallel."""
    api = agent["api"]
    card: dict[str, Any] = {"name": agent["name"], "api": api}

    status = _fetch_json(f"{api}/api/status")
    if status is None:
        card["error"] = "unreachable"
        return card

    if not isinstance(status, dict):
        card["error"] = "invalid status response"
        return card

    card["status"] = status

    # Fetch remaining endpoints in parallel using a single shared pool
    with ThreadPoolExecutor(max_workers=3) as sub_ex:
        fut_tasks = sub_ex.submit(_fetch_json, f"{api}/api/tasks?state=active")
        fut_services = sub_ex.submit(_fetch_json, f"{api}/api/services")
        fut_sessions = sub_ex.submit(_fetch_json, f"{api}/api/sessions?limit=1")
        tasks_data = fut_tasks.result()
        services_data = fut_services.result()
        sessions_data = fut_sessions.result()

    if isinstance(tasks_data, list):
        card["active_tasks"] = len(tasks_data)
    elif isinstance(tasks_data, dict) and isinstance(tasks_data.get("tasks"), list):
        card["active_tasks"] = len(tasks_data["tasks"])
    else:
        card["active_tasks"] = None

    if isinstance(services_data, dict):
        svcs = services_data.get("services") or []
        card["running_services"] = [
            s["name"] for s in svcs if isinstance(s, dict) and s.get("active") and "name" in s
        ]
    else:
        card["running_services"] = None

    if isinstance(sessions_data, dict):
        sessions = sessions_data.get("sessions")
        sessions = sessions if isinstance(sessions, list) else []
        first = sessions[0] if sessions else None
        card["last_session"] = first.get("date") if isinstance(first, dict) else None
    else:
        card["last_session"] = None

    return card


def create_app(
    workspace: Path, site_dir: Path | None = None, org_config: Path | None = None
) -> Any:
    """Create Flask app serving static dashboard + API.

    Args:
        workspace: Path to the gptme workspace root.
        site_dir: Directory containing the generated static site.
            If None, generates into ``<workspace>/_site``.
        org_config: Optional path to an org TOML config listing remote agents.
            When provided, enables the ``/api/org`` aggregation endpoint and
            serves ``/org`` as the org view page.

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
    from .generate import (
        _parse_toml,
        detect_submodules,
        generate,
        read_agent_urls,
        read_workspace_config,
        scan_journals,
        scan_lessons,
        scan_skills,
        scan_summaries,
        scan_tasks,
    )

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

    # Load org config if provided
    _org_agents: list[dict[str, str]] = []
    if org_config is not None:
        try:
            _org_agents = load_org_config(org_config)
            logger.info("Org config loaded: %d agents", len(_org_agents))
        except Exception as e:
            logger.error("Failed to load org config %s: %s", org_config, e)
            raise

    @app.route("/")
    def index() -> Any:
        return app.send_static_file("index.html")

    @app.route("/api/status")
    def api_status() -> Any:
        ws = Path(app.config["WORKSPACE"])
        try:
            # Parse gptme.toml once and share the data to avoid double reads.
            toml_data = _parse_toml(ws / "gptme.toml")
            config = read_workspace_config(ws, _data=toml_data)
            urls = read_agent_urls(ws, _data=toml_data)
            return jsonify(
                {
                    "mode": "dynamic",
                    "agent": config.get("agent_name", ws.name),
                    "workspace": ws.name,
                    "urls": urls,
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
    _health_cache_lock = threading.Lock()
    # UINT64_MAX: systemd returns this sentinel when MemoryAccounting is disabled
    # or no cgroup data is available. Treat as "no data" rather than ~17.2 EB.
    _UINT64_MAX = 18446744073709551615

    # Restart token — read from env → gptme.toml [dashboard] → auto-generate.
    # Printed at startup so the user can find it in the server log.
    import os as _os
    import secrets as _secrets

    _MIN_TOKEN_LEN = 32
    _restart_token = _os.environ.get("GPTME_DASHBOARD_RESTART_TOKEN", "")
    if _restart_token and len(_restart_token) < _MIN_TOKEN_LEN:
        logger.warning(
            "GPTME_DASHBOARD_RESTART_TOKEN is too short (%d chars, minimum %d); "
            "falling back to auto-generated token",
            len(_restart_token),
            _MIN_TOKEN_LEN,
        )
        _restart_token = ""
    if not _restart_token:
        try:
            import tomllib as _tomllib

            _toml_path = workspace / "gptme.toml"
            if _toml_path.exists():
                with open(_toml_path, "rb") as _f:
                    _toml_cfg = _tomllib.load(_f)
                _restart_token = _toml_cfg.get("dashboard", {}).get("restart_token", "")
                if _restart_token and len(_restart_token) < _MIN_TOKEN_LEN:
                    logger.warning(
                        "gptme.toml [dashboard] restart_token is too short (%d chars, minimum %d); "
                        "falling back to auto-generated token",
                        len(_restart_token),
                        _MIN_TOKEN_LEN,
                    )
                    _restart_token = ""
        except ModuleNotFoundError:
            logger.debug("tomllib not available (Python < 3.11); skipping gptme.toml restart_token")
        except Exception as _e:
            logger.warning("Failed to read restart_token from gptme.toml: %s", _e)
    if not _restart_token:
        _restart_token = _secrets.token_hex(32)
        # NOTE: _restart_token is per-process. In multi-worker deployments (Gunicorn,
        # uWSGI), each worker generates its own token independently, causing 403s when
        # the browser POSTs to a different worker than it fetched from. This is a
        # known limitation of the current single-process dev-server use case. If a
        # multi-worker deployment is ever needed, set GPTME_DASHBOARD_RESTART_TOKEN
        # from an external source so all workers share the same token.
        logger.info(
            "Dashboard restart token (auto-generated): %s",
            _restart_token,
        )

    @app.route("/api/services")
    def api_services() -> Any:
        """Return systemd/launchd service status for gptme-related services.

        Detects services whose names contain "gptme" or the agent name from
        gptme.toml.  Works on Linux (systemd --user) and macOS (launchctl).
        Returns an empty list gracefully when detection is unavailable.
        """
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
                        units = json.loads(result.stdout)
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
                except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
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
                        units = json.loads(result.stdout)
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
                    json.JSONDecodeError,
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
        from datetime import datetime, timezone

        now = time.monotonic()
        with _health_cache_lock:
            cached = _health_cache["data"]
            if cached is not None and now < _health_cache["expires"]:
                status = 500 if "error" in cached else 200
                return jsonify(cached), status

        ws = Path(app.config["WORKSPACE"])
        system = platform.system()

        if system != "Linux":
            result: dict[str, Any] = {"services": [], "platform": system}
            with _health_cache_lock:
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
                    with _health_cache_lock:
                        _health_cache["data"] = result
                        _health_cache["expires"] = now + _HEALTH_CACHE_TTL
                    return jsonify(result)

                units = json.loads(list_result.stdout)
                relevant = [u for u in units if _is_relevant_service(u.get("unit", ""), agent_name)]
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
                result = {"services": [], "platform": system}
                with _health_cache_lock:
                    _health_cache["data"] = result
                    _health_cache["expires"] = now + _HEALTH_CACHE_TTL
                return jsonify(result)

            health_list: list[dict[str, Any]] = []
            utcnow = datetime.now(timezone.utc)
            # Hoist out of loop — same frozenset every iteration
            _active_states = frozenset({"active", "reloading", "activating", "deactivating"})

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
                        # Strip the weekday prefix; use timezone abbreviation if present.
                        parts = active_since.split()
                        if len(parts) >= 3:
                            dt_str = parts[1] + " " + parts[2]
                            tz_abbrev = parts[3] if len(parts) >= 4 else "UTC"
                            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                            if tz_abbrev in ("UTC", "GMT"):
                                dt = dt.replace(tzinfo=timezone.utc)
                            else:
                                # systemd reports in local time; use system offset
                                local_tz = datetime.now().astimezone().tzinfo
                                dt = dt.replace(tzinfo=local_tz)
                            uptime_seconds = max(0, int((utcnow - dt).total_seconds()))
                    except (ValueError, IndexError):
                        pass

                # Parse PID
                pid = 0
                try:
                    pid = int(props.get("MainPID", "0"))
                except ValueError:
                    pass

                # Parse memory (MemoryCurrent is in bytes, may be "[not set]" or
                # UINT64_MAX when MemoryAccounting is disabled or service is stopped)
                memory_bytes = 0
                mem_str = props.get("MemoryCurrent", "")
                if mem_str and mem_str not in ("[not set]", "infinity"):
                    try:
                        parsed = int(mem_str)
                        if parsed != _UINT64_MAX:
                            memory_bytes = parsed
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
                        recent_errors = len(lines)
                except (OSError, subprocess.TimeoutExpired):
                    pass

                # Classify health
                # Treat transient systemd states (reloading/activating/deactivating)
                # as non-unhealthy — they are normal lifecycle transitions, not failures.
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
            with _health_cache_lock:
                _health_cache["data"] = result
                _health_cache["expires"] = now + _HEALTH_CACHE_TTL
            return jsonify(result)
        except Exception as e:
            logger.exception("Error computing service health")
            # Cache error response briefly (10s) to avoid re-running expensive
            # subprocess calls on every request during a persistent failure.
            # Store error key so cache-hit path can replay the 500 status.
            with _health_cache_lock:
                _health_cache["data"] = {"error": str(e), "platform": system}
                _health_cache["expires"] = now + 10.0
            return jsonify({"error": str(e)}), 500

    @app.route("/api/services/restart-enabled")
    def api_services_restart_enabled() -> Any:
        """Return whether restart actions are enabled and the session token.

        Only responds to localhost requests. Cross-origin JS cannot read this
        response (CORS blocked by browsers), so returning the token here is
        safe — it follows the standard CSRF token pattern.
        """
        try:
            _is_loopback = ipaddress.ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            _is_loopback = False
        if not _is_loopback:
            return jsonify({"enabled": False, "token": None}), 403
        resp = jsonify({"enabled": True, "token": _restart_token})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/services/<name>/restart", methods=["POST"])
    def api_service_restart(name: str) -> Any:
        """Restart a managed systemd service.

        Security model (defense in depth):
        1. Loopback-only: reject non-localhost requests.
        2. Token check: ``X-Restart-Token`` header must match server token.
        3. Whitelist: service name must pass ``_is_relevant_service()``.
        4. Execute: ``systemctl --user restart <name>``.
        """
        # 1. Loopback check
        try:
            _is_loopback = ipaddress.ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            _is_loopback = False
        if not _is_loopback:
            return jsonify(
                {"error": "Restart only allowed from localhost", "status": "forbidden"}
            ), 403

        # 2. Token check (constant-time comparison to avoid timing attacks)
        token = request.headers.get("X-Restart-Token", "")
        if not _secrets.compare_digest(token.encode(), _restart_token.encode()):
            return jsonify({"error": "Invalid restart token", "status": "forbidden"}), 403

        # 3. Whitelist check
        ws = Path(app.config["WORKSPACE"])
        agent_name = _get_agent_name(ws)
        if not _is_relevant_service(name, agent_name):
            return (
                jsonify({"error": "Service not allowed", "status": "forbidden", "service": name}),
                403,
            )

        # 4. Linux/systemd only
        if platform.system() != "Linux":
            return (
                jsonify(
                    {"error": "Restart only supported on Linux/systemd", "status": "unsupported"}
                ),
                501,
            )

        # 5. Execute restart
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", name],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                logger.info("Restarted service %s via dashboard", name)
                return jsonify(
                    {
                        "service": name,
                        "action": "restart",
                        "status": "ok",
                        "message": "Service restart triggered",
                    }
                )
            else:
                err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                logger.warning("Failed to restart %s: %s", name, err)
                return jsonify({"service": name, "error": err, "status": "error"}), 500
        except subprocess.TimeoutExpired:
            return jsonify({"service": name, "error": "Restart timed out", "status": "error"}), 500
        except OSError as e:
            return jsonify({"service": name, "error": str(e), "status": "error"}), 500

    # Cache for logs endpoint (per-service, 30s TTL)
    _LOGS_CACHE_TTL = 30
    _logs_cache: dict[str, Any] = {}
    _logs_cache_lock = threading.Lock()

    # Syslog priority names (RFC 5424)
    _PRIORITY_NAMES = {
        "0": "emerg",
        "1": "alert",
        "2": "crit",
        "3": "err",
        "4": "warning",
        "5": "notice",
        "6": "info",
        "7": "debug",
    }

    @app.route("/api/services/logs")
    def api_services_logs() -> Any:
        """Return recent log entries for a specific gptme-related service.

        Query parameters:
        - service (required): service unit name (e.g. "bob-autonomous.service")
        - since: time window (default "1h", supports "1h", "6h", "24h", "7d")
        - lines: max lines to return (default 100, max 500)
        - priority: minimum priority filter ("err", "warning", "info", "debug")

        Uses a 30-second per-service cache to avoid expensive journalctl calls.
        Linux (systemd --user) only; returns empty on other platforms.
        """
        service = request.args.get("service", "").strip()
        if not service:
            return jsonify({"error": "Missing required 'service' parameter"}), 400

        since = request.args.get("since", "1h").strip()
        valid_since = {
            "1h": "1 hour ago",
            "6h": "6 hours ago",
            "24h": "24 hours ago",
            "7d": "7 days ago",
        }
        if since not in valid_since:
            return (
                jsonify(
                    {
                        "error": f"Invalid 'since' value '{since}'. Must be one of: {', '.join(valid_since)}"
                    }
                ),
                400,
            )

        lines = request.args.get("lines", 100, type=int)
        lines = max(1, min(lines, 500))

        priority_filter = request.args.get("priority", "").strip()
        valid_priorities = {"err": "3", "warning": "4", "notice": "5", "info": "6", "debug": "7"}
        if priority_filter and priority_filter not in valid_priorities:
            return (
                jsonify(
                    {
                        "error": f"Invalid 'priority' value '{priority_filter}'. Must be one of: {', '.join(valid_priorities)}"
                    }
                ),
                400,
            )

        # Validate service name is gptme/agent-related (security: prevent arbitrary unit queries)
        # Must happen before the platform check so non-Linux hosts also reject disallowed services.
        ws = Path(app.config["WORKSPACE"])
        agent_name = _get_agent_name(ws)
        if not _is_relevant_service(service, agent_name):
            return jsonify(
                {"error": f"Service '{service}' is not a recognized gptme/agent service"}
            ), 403

        system = platform.system()
        if system != "Linux":
            return jsonify({"logs": [], "service": service, "platform": system})

        # Check cache
        now = time.monotonic()
        cache_key = f"{service}:{since}:{lines}:{priority_filter}"
        with _logs_cache_lock:
            cached = _logs_cache.get(cache_key)
            if cached is not None and now < cached["expires"]:
                return jsonify(cached["data"])

        try:
            cmd = [
                "journalctl",
                "--user",
                "-u",
                service,
                "--since",
                valid_since[since],
                "-n",
                str(lines),
                "--no-pager",
                "--output=json",
            ]
            if priority_filter:
                cmd.extend(["--priority", valid_priorities[priority_filter]])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            log_entries: list[dict[str, Any]] = []
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    try:
                        entry = json.loads(line)
                        # Extract fields from systemd journal JSON
                        ts_us = int(entry.get("__REALTIME_TIMESTAMP", "0"))
                        prio = str(entry.get("PRIORITY", "6"))
                        raw_msg = entry.get("MESSAGE", "")
                        # journalctl encodes binary MESSAGE fields as int arrays
                        if isinstance(raw_msg, list):
                            message: str = bytes(raw_msg).decode("utf-8", errors="replace")
                        else:
                            message = raw_msg
                        log_entries.append(
                            {
                                "timestamp": ts_us / 1_000_000,  # epoch seconds (float)
                                "priority": _PRIORITY_NAMES.get(prio, prio),
                                "message": message,
                            }
                        )
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue

            response_data: dict[str, Any] = {
                "logs": log_entries,
                "service": service,
                "total": len(log_entries),
                "since": since,
                "platform": system,
            }
            # Non-zero exit means journalctl failed (unit not found, no access, etc.)
            # Include a warning so callers can distinguish from "no recent logs".
            if result.returncode != 0:
                stderr_snippet = result.stderr.strip()[:200] if result.stderr else ""
                response_data["warning"] = f"journalctl exited with code {result.returncode}" + (
                    f": {stderr_snippet}" if stderr_snippet else ""
                )

            now_after = time.monotonic()  # fresh snapshot after subprocess to avoid stale TTL
            with _logs_cache_lock:
                # Evict expired entries to prevent unbounded growth
                expired_keys = [k for k, v in _logs_cache.items() if now_after >= v["expires"]]
                for k in expired_keys:
                    del _logs_cache[k]
                _logs_cache[cache_key] = {
                    "data": response_data,
                    "expires": now_after + _LOGS_CACHE_TTL,
                }

            return jsonify(response_data)
        except subprocess.TimeoutExpired:
            timeout_data: dict[str, Any] = {
                "logs": [],
                "service": service,
                "total": 0,
                "since": since,
                "platform": system,
            }
            now_timeout = time.monotonic()
            with _logs_cache_lock:
                expired_keys = [k for k, v in _logs_cache.items() if now_timeout >= v["expires"]]
                for k in expired_keys:
                    del _logs_cache[k]
                _logs_cache[cache_key] = {
                    "data": timeout_data,
                    "expires": now_timeout + _LOGS_CACHE_TTL,
                }
            return jsonify(timeout_data)
        except Exception as e:
            logger.exception("Error fetching service logs")
            # Cache the empty result so persistent errors (e.g. journalctl not found)
            # don't spawn a new subprocess on every request.
            error_data: dict[str, Any] = {
                "logs": [],
                "service": service,
                "total": 0,
                "since": since,
                "platform": system,
            }
            with _logs_cache_lock:
                _logs_cache[cache_key] = {
                    "data": error_data,
                    "expires": time.monotonic() + _LOGS_CACHE_TTL,
                }
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

    # --- Search ---
    # In-memory search index: cached for SEARCH_CACHE_TTL seconds
    _SEARCH_CACHE_TTL = 300
    _search_cache: dict[str, Any] = {"data": None, "expires": 0.0}
    _search_cache_lock = threading.Lock()

    def _build_search_index(ws: Path) -> "list[dict[str, Any]]":
        """Build a unified list of searchable items from the workspace."""
        items: list[dict[str, Any]] = []

        def _add_lessons(lessons: "list[dict[str, Any]]") -> None:
            for lesson in lessons:
                items.append(
                    {
                        "type": "lesson",
                        "title": lesson.get("title", ""),
                        "category": lesson.get("category", ""),
                        "keywords": lesson.get("all_keywords", []),
                        "tags": [],
                        "excerpt": lesson.get("body", "")[:600],
                        "url": "/" + lesson.get("page_url", ""),
                        "path": lesson.get("path", ""),
                    }
                )

        def _add_skills(skills: "list[dict[str, Any]]") -> None:
            for skill in skills:
                items.append(
                    {
                        "type": "skill",
                        "title": skill.get("title", "") or skill.get("name", ""),
                        "category": skill.get("category", ""),
                        "keywords": skill.get("keywords", []),
                        "tags": skill.get("tags", []),
                        "excerpt": (skill.get("description", "") + " " + skill.get("body", ""))[
                            :600
                        ],
                        "url": "/" + skill.get("page_url", ""),
                        "path": skill.get("path", ""),
                    }
                )

        # Main workspace lessons and skills
        _add_lessons(scan_lessons(ws))
        _add_skills(scan_skills(ws))

        # Submodule lessons and skills (e.g. gptme-contrib, gptme-superuser)
        for sub in detect_submodules(ws):
            sub_path = sub["abs_path"]
            sub_name = sub["name"]
            if sub.get("has_lessons"):
                _add_lessons(scan_lessons(sub_path, source=sub_name))
            if sub.get("has_skills"):
                _add_skills(scan_skills(sub_path, source=sub_name))

        # Tasks — limit to 500 to keep index size bounded
        for task in scan_tasks(ws)[:500]:
            items.append(
                {
                    "type": "task",
                    "title": task.get("title", ""),
                    "category": task.get("state", ""),
                    "keywords": [],
                    "tags": task.get("tags", []),
                    "excerpt": task.get("body", "")[:600],
                    "url": "/" + task.get("page_url", ""),
                    "path": task.get("path", ""),
                }
            )

        # Journals — use all available (limit=500 to get recent history)
        for journal in scan_journals(ws, limit=500):
            items.append(
                {
                    "type": "journal",
                    "title": journal.get("name", "") or journal.get("date", ""),
                    "category": journal.get("date", ""),
                    "keywords": [],
                    "tags": [],
                    "excerpt": journal.get("body", "")[:600],
                    "url": "/" + journal.get("page_url", ""),
                    "path": journal.get("path", ""),
                }
            )

        # Summaries
        for summary in scan_summaries(ws, limit=100):
            items.append(
                {
                    "type": "summary",
                    "title": summary.get("title", "") or summary.get("period", ""),
                    "category": summary.get("type", ""),
                    "keywords": [],
                    "tags": [],
                    "excerpt": summary.get("body", "")[:600],
                    "url": "/" + summary.get("page_url", ""),
                    "path": summary.get("path", ""),
                }
            )

        return items

    def _score_item(item: "dict[str, Any]", query_words: "list[str]") -> int:
        """Score an item by how well it matches query_words. Higher = more relevant."""
        score = 0
        title = item.get("title", "").lower()
        excerpt = item.get("excerpt", "").lower()
        kw_text = " ".join(item.get("keywords", [])).lower()
        tag_text = " ".join(item.get("tags", [])).lower()
        category = item.get("category", "").lower()

        title_words = set(re.sub(r"[^\w\s]", " ", title).split())
        kw_words = set(re.sub(r"[^\w\s]", " ", kw_text).split())
        tag_words = set(re.sub(r"[^\w\s]", " ", tag_text).split())
        category_words = set(re.sub(r"[^\w\s]", " ", category).split())
        excerpt_words = set(re.sub(r"[^\w\s]", " ", excerpt).split())

        for w in query_words:
            # Title exact word match
            if w in title_words:
                score += 20
            elif any(tw.startswith(w) for tw in title_words):
                score += 8
            # Keyword match (highly relevant for lessons) — word-boundary to avoid "it" → "activities"
            if w in kw_words:
                score += 6
            # Tag match
            if w in tag_words:
                score += 4
            # Category/state match
            if w in category_words:
                score += 2
            # Body/excerpt match (low weight, broad)
            if w in excerpt_words:
                score += 1

        return score

    _SEARCH_VALID_TYPES = frozenset({"lesson", "skill", "task", "journal", "summary"})

    @app.route("/api/search")
    def api_search() -> Any:
        """Full-text search across workspace content.

        Query parameters:
            q (str): Search query, minimum 2 characters. Required.
            type (str): Filter to a specific content type.
                One of: lesson, skill, task, journal, summary.
            limit (int): Maximum results to return (1–100, default 20).

        Returns:
            JSON with ``results`` list, ``total`` count, ``query``, and
            ``type_filter`` (null when not filtered).
        """
        ws = Path(app.config["WORKSPACE"])
        try:
            query = (request.args.get("q") or "").strip()
            query_words = re.sub(r"[^\w\s]", " ", query.lower()).split()
            if len(query) < 2 or not query_words:
                return jsonify({"error": "Query must be at least 2 characters"}), 400

            type_filter = (request.args.get("type") or "").lower()
            if type_filter and type_filter not in _SEARCH_VALID_TYPES:
                return jsonify(
                    {
                        "error": (
                            f"Invalid type '{type_filter}'. "
                            f"Must be one of: {', '.join(sorted(_SEARCH_VALID_TYPES))}"
                        )
                    }
                ), 400

            limit = request.args.get("limit", 20, type=int)
            limit = max(1, min(limit, 100))

            # Build or refresh the search index outside the lock to avoid stalling
            # concurrent requests during the (potentially slow) workspace scan.
            now = time.monotonic()
            needs_rebuild = False
            with _search_cache_lock:
                # Use expires as the sole rebuild gate: once claimed (set to a future
                # time), concurrent threads — including those seeing data=None on a cold
                # cache — all skip the rebuild and get [] until the index is ready.
                needs_rebuild = now >= _search_cache["expires"]
                if needs_rebuild:
                    # Claim the slot so other threads skip the rebuild
                    _search_cache["expires"] = time.monotonic() + _SEARCH_CACHE_TTL

            if needs_rebuild:
                try:
                    new_index = _build_search_index(ws)
                    with _search_cache_lock:
                        _search_cache["data"] = new_index
                except Exception:
                    # Release the slot so the next request retries the build
                    with _search_cache_lock:
                        _search_cache["expires"] = 0.0
                    raise

            with _search_cache_lock:
                items: list[dict[str, Any]] = _search_cache["data"] or []
            if type_filter:
                items = [i for i in items if i["type"] == type_filter]

            scored: list[tuple[int, dict[str, Any]]] = []
            for item in items:
                score = _score_item(item, query_words)
                if score > 0:
                    scored.append((score, item))

            scored.sort(key=lambda x: x[0], reverse=True)
            total = len(scored)
            results = [dict(item) for _, item in scored[:limit]]

            return jsonify(
                {
                    "results": results,
                    "total": total,
                    "query": query,
                    "type_filter": type_filter or None,
                }
            )
        except Exception as e:
            logger.exception("Error searching workspace")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/org")
    def api_org() -> Any:
        """Aggregate status from all known agents in the org.

        Returns a list of agent cards, each containing:
        - ``name``: agent name from org.toml
        - ``api``: base API URL
        - ``status``: agent name/workspace from /api/status (or null on error)
        - ``tasks``: active task count from /api/tasks?state=active (or null)
        - ``services``: running service names from /api/services (or null)
        - ``sessions``: most recent session timestamp from /api/sessions (or null)
        - ``error``: error message if the agent API was unreachable

        Returns 404 if no org config was loaded.
        """
        if not _org_agents:
            msg = (
                f"Org config loaded from {org_config} but contains no agents"
                if org_config is not None
                else "No org config loaded. Start server with --org <org.toml>"
            )
            return jsonify({"error": msg}), 404

        # Fetch all agents in parallel
        cards: list[dict[str, Any]] = [None] * len(_org_agents)  # type: ignore[list-item]
        with ThreadPoolExecutor(max_workers=min(10, len(_org_agents))) as ex:
            futures = {ex.submit(_fetch_agent_card, a): i for i, a in enumerate(_org_agents)}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    cards[idx] = fut.result()
                except Exception as e:
                    agent = _org_agents[idx]
                    logger.warning("Agent card fetch failed for %s: %s", agent["name"], e)
                    cards[idx] = {"name": agent["name"], "api": agent["api"], "error": str(e)}

        return jsonify({"agents": cards, "count": len(cards)})

    # Org page template — defined once at create_app scope, not per-request
    _ORG_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Org View</title>
  <style>
    :root { --bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--text-dim:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922; }
    * { box-sizing:border-box; margin:0; padding:0; }
    body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; padding:2rem; }
    h1 { font-size:1.5rem; margin-bottom:1.5rem; }
    h1 span { color:var(--text-dim); font-weight:400; }
    .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:1rem; }
    .card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:1.25rem; }
    .card h2 { font-size:1.1rem; margin-bottom:0.75rem; }
    .card h2 a { color:var(--accent); text-decoration:none; }
    .status { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }
    .status.active { background:var(--green); }
    .status.unreachable { background:var(--red); }
    .row { display:flex; justify-content:space-between; font-size:0.875rem; margin-top:0.5rem; color:var(--text-dim); }
    .row span:last-child { color:var(--text); }
    .error { color:var(--red); font-size:0.875rem; margin-top:0.5rem; }
    #status-bar { margin-bottom:1.5rem; font-size:0.875rem; color:var(--text-dim); }
    #status-bar strong { color:var(--text); }
  </style>
</head>
<body>
  <h1>Org View <span id="org-summary"></span></h1>
  <div id="status-bar">Loading agents...</div>
  <div class="grid" id="agent-grid"></div>
  <script>
    function esc(s) {
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    async function loadOrg() {
      try {
        const resp = await fetch('/api/org');
        if (!resp.ok) { document.getElementById('status-bar').textContent = 'Failed to load org data'; return; }
        const data = await resp.json();
        const agents = data.agents || [];
        const active = agents.filter(a => !a.error).length;
        document.getElementById('org-summary').textContent = '(' + agents.length + ' agents)';
        document.getElementById('status-bar').innerHTML = '<strong>' + active + '</strong> reachable, <strong>' + (agents.length - active) + '</strong> unreachable';
        const grid = document.getElementById('agent-grid');
        grid.innerHTML = agents.map(a => {
          const reachable = !a.error;
          const name = esc(a.status ? (a.status.agent || a.name) : a.name);
          const apiLink = '<a href="' + esc(a.api) + '" target="_blank" rel="noopener noreferrer">' + name + '</a>';
          const dot = '<span class="status ' + (reachable ? 'active' : 'unreachable') + '"></span>';
          let body = '';
          if (!reachable) {
            body = '<div class="error">Unreachable</div>';
          } else {
            if (a.active_tasks !== null) body += '<div class="row"><span>Active tasks</span><span>' + a.active_tasks + '</span></div>';
            if (a.running_services !== null) body += '<div class="row"><span>Running services</span><span>' + (a.running_services.length ? a.running_services.map(esc).join(', ') : 'none') + '</span></div>';
            if (a.last_session) body += '<div class="row"><span>Last session</span><span>' + esc(a.last_session) + '</span></div>';
          }
          return '<div class="card"><h2>' + dot + apiLink + '</h2>' + body + '</div>';
        }).join('');
      } catch(e) {
        document.getElementById('status-bar').textContent = 'Error: ' + e.message;
      }
    }
    loadOrg();
    setInterval(loadOrg, 30000);
  </script>
</body>
</html>"""

    @app.route("/org")
    @app.route("/org.html")
    def org_view() -> Any:
        """Serve the org view page (agent grid)."""
        if not _org_agents:
            if org_config is not None:
                msg = f"Org config {org_config} contains no agents"
            else:
                msg = "No org config loaded. Start the server with --org &lt;org.toml&gt;"
            return (
                f"<html><body><h1>No org config</h1><p>{msg}</p></body></html>",
                404,
            )
        return _ORG_PAGE

    return app
