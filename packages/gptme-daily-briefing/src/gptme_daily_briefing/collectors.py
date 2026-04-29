"""Generic collectors for daily-briefing bundles.

Each collector is a small, side-effect-free function that returns a piece
of the bundle. Agents compose them in their own wrapper script.

Design constraint: nothing here may import agent-local packages
(metaproductivity, Bob-specific scripts, agent-specific KPIs). Such
collectors stay in each agent's local wrapper.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
    """Run a command, return stripped stdout or empty string on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"[warn] {cmd[0]}: {e}", file=sys.stderr)
        return ""


def collect_graphql_rate_limit() -> dict[str, Any] | None:
    """Return GitHub GraphQL rate-limit info via REST, or None on failure."""
    out = _run(["gh", "api", "rate_limit"], timeout=10)
    if not out:
        return None
    try:
        resources = json.loads(out).get("resources", {})
    except json.JSONDecodeError:
        return None
    graphql = resources.get("graphql")
    return graphql if isinstance(graphql, dict) else None


def collect_blockers(repo: str, label: str, limit: int = 6) -> list[str]:
    """Open issues with `label` in `repo` (excludes PRs).

    Returns formatted strings like ``"#570: Title"``. The label is
    URL-encoded so values containing spaces (``"help wanted"``) or
    ampersands work correctly.
    """
    encoded_label = quote(label, safe="")
    out = _run(
        [
            "gh",
            "api",
            f"repos/{repo}/issues?labels={encoded_label}&state=open&per_page={limit}",
        ]
    )
    if not out:
        return []
    try:
        blockers = [
            f"#{item['number']}: {item['title']}"
            for item in json.loads(out)
            if "pull_request" not in item
        ]
        return blockers[:limit]
    except (json.JSONDecodeError, KeyError):
        return []


def collect_active_tasks(workspace_root: Path, limit: int = 6) -> list[str]:
    """Active and todo task ids via `gptodo list --json` (run inside workspace_root)."""
    out = _run(["uv", "run", "gptodo", "list", "--json"], cwd=workspace_root)
    if not out:
        return []
    try:
        tasks = json.loads(out).get("tasks", [])
        return [t["id"] for t in tasks if t.get("state") in ("active", "todo") and t.get("id")][
            :limit
        ]
    except (json.JSONDecodeError, KeyError):
        return []


def collect_waiting_tasks(workspace_root: Path, limit: int = 8) -> list[dict[str, str]]:
    """Waiting tasks with `waiting_for`, parsed from ``<workspace>/tasks/*.md`` frontmatter.

    `gptodo list --json` doesn't currently surface ``waiting_for``, so we read
    the YAML frontmatter directly.
    """
    tasks_dir = workspace_root / "tasks"
    if not tasks_dir.is_dir():
        return []
    result: list[dict[str, str]] = []
    for task_file in sorted(tasks_dir.glob("*.md")):
        try:
            text = task_file.read_text()
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        if fm.get("state") != "waiting":
            continue
        wf = fm.get("waiting_for")
        if wf:
            result.append({"task": task_file.stem, "waiting_for": str(wf)[:200]})
    return result[:limit]


def collect_recent_highlights(workspace_root: Path, limit: int = 6) -> list[str]:
    """Recent commit subjects from the agent's main integration branch.

    Tries refs in order: ``origin/master`` → ``origin/main`` → ``master`` →
    ``main``. Explicitly avoids logging the bare current ``HEAD`` because on
    a feature branch that would silently surface off-topic commits instead
    of landed integration work.
    """
    fetch_n = max(limit * 2, 10)
    for ref in ("origin/master", "origin/main", "master", "main"):
        out = _run(
            ["git", "log", "--pretty=format:%s", f"-{fetch_n}", ref],
            cwd=workspace_root,
        )
        if out:
            return [line.strip() for line in out.splitlines() if line.strip()][:limit]
    return []


def collect_session_stats(sessions_dir: Path, days: int = 1) -> dict[str, Any]:
    """Session count and category distribution over the last ``days`` days.

    Requires the ``gptme-sessions`` package — install via the ``[sessions]`` extra.
    On any failure (missing package, bad records), returns a stats dict with
    ``count: 0`` and an ``error`` field instead of raising.
    """
    try:
        from gptme_sessions.store import SessionStore  # type: ignore[import-not-found]

        store = SessionStore(sessions_dir=sessions_dir)
        records = store.load_all()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        recent = []
        for r in records:
            try:
                ts = datetime.fromisoformat(r.timestamp.replace("Z", "+00:00"))
                if ts >= cutoff:
                    recent.append(r)
            except Exception:
                continue

        cats: dict[str, int] = {}
        for r in recent:
            cat = getattr(r, "category", None) or "unknown"
            cats[cat] = cats.get(cat, 0) + 1

        return {"count": len(recent), "categories": cats}
    except Exception as e:
        return {"count": 0, "error": str(e)}


def collect_open_prs(
    repos: list[str],
    username: str,
    limit_per_repo: int = 5,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """Open PRs by ``username`` across the given repositories.

    Paginates through ``state=open&per_page=100`` results so users with PRs
    beyond the first 100 in a repo aren't silently dropped. Stops at the
    first empty page or when ``max_pages`` is reached (default 5 → up to
    500 PRs scanned per repo).
    """
    prs: list[dict[str, Any]] = []
    for repo in repos:
        repo_prs: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            out = _run(["gh", "api", f"repos/{repo}/pulls?state=open&per_page=100&page={page}"])
            if not out:
                break
            try:
                batch = json.loads(out)
            except json.JSONDecodeError:
                break
            if not batch:
                break
            try:
                for pr in batch:
                    if pr.get("user", {}).get("login") != username:
                        continue
                    repo_prs.append(
                        {
                            "repo": repo,
                            "number": pr["number"],
                            "title": pr["title"],
                            "draft": pr.get("draft", False),
                            "url": f"https://github.com/{repo}/pull/{pr['number']}",
                        }
                    )
            except KeyError:
                break
            if len(batch) < 100:
                break
        prs.extend(repo_prs[:limit_per_repo])
    return prs
