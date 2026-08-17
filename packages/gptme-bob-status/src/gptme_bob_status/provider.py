"""Bob-workspace StatusProvider for ``gptme-util status``.

Registers as the ``bob`` entry point under the ``gptme.status_providers``
group.  Only contributes data when running inside Bob's workspace (detected by
the presence of a ``tasks/`` directory and ``gptme.toml``).  Returns empty
data everywhere else so the provider is safe to install globally.

Entry-point registration (``pyproject.toml``):

.. code-block:: toml

    [project.entry-points."gptme.status_providers"]
    bob = "gptme_bob_status.provider:make_provider"
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

try:
    from gptme.status_provider import StatusProvider
except ImportError:
    # Fallback: define StatusProvider as a Protocol if not available in gptme
    @runtime_checkable
    class StatusProvider(Protocol):
        """Protocol for status providers."""

        name: str

        def collect(self) -> dict[str, Any]:
            """Collect status data."""
            ...

        def narrative_sections(self) -> list[str]:
            """Generate narrative status sections."""
            ...


try:
    from gptme.util.git_cmd import GIT_CMD
except ImportError:
    GIT_CMD = "git"

logger = logging.getLogger(__name__)

_TRACKED_REPOS: list[tuple[str, int | None]] = [
    ("gptme/gptme", 10),
    ("gptme/gptme-contrib", None),
    ("gptme/gptme-cloud", 3),
    ("ErikBjare/bob", None),
]


def _run(cmd: list[str], *, timeout: int = 10) -> str:
    try:
        return subprocess.check_output(
            cmd, text=True, stderr=subprocess.DEVNULL, timeout=timeout
        ).strip()
    except Exception:
        return ""


def _git_root() -> Path | None:
    raw = _run([GIT_CMD, "rev-parse", "--show-toplevel"])
    return Path(raw) if raw else None


def _is_bob_workspace() -> bool:
    root = _git_root() or Path.cwd()
    return (root / "tasks").is_dir() and (root / "gptme.toml").is_file()


def _active_tasks(lines: int = 3) -> list[dict]:
    raw = _run(["gptodo", "status", "--compact"], timeout=15)
    if not raw:
        return []
    tasks: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("📋") or "0 tasks" in line or "Summary" in line:
            continue
        parts = line.split(None, 1)
        if len(parts) >= 2:
            task_id = parts[0]
            title = re.sub(r"\s+\(\d+\s+\w+\s+ago\)\s*$", "", parts[1]).strip()
            tasks.append({"id": task_id, "title": title})
    return tasks[:lines]


def _pr_queue() -> list[dict]:
    author = _run(["gh", "api", "user", "--jq", ".login"], timeout=10)
    if not author:
        return []
    rows: list[dict] = []
    for repo, cap in _TRACKED_REPOS:
        prs_json = _run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--author",
                author,
                "--state",
                "open",
                "--json",
                "number,title",
            ],
            timeout=15,
        )
        if not prs_json:
            continue
        try:
            prs = json.loads(prs_json)
        except json.JSONDecodeError:
            continue
        rows.append({"repo": repo, "count": len(prs), "cap": cap})
    return rows


def _service_status() -> list[dict]:
    services = [
        ("Operator loop", "bob-operator-loop.service"),
        ("Autonomous", "bob-autonomous.service"),
    ]
    results: list[dict] = []
    for label, unit in services:
        status = _run(["systemctl", "--user", "is-active", unit])
        icon = "✓" if status == "active" else ("⚠" if status == "activating" else "✗")
        results.append({"label": label, "icon": icon, "status": status})
    return results


def _dead_timers() -> int:
    out = _run(["systemctl", "--user", "list-timers", "--all"])
    return sum(
        1
        for line in out.splitlines()
        if "dead" in line.lower() and "bob-" in line.lower()
    )


def _blockers(limit: int = 3) -> list[dict]:
    raw = _run(["gptodo", "ready", "--state", "waiting", "--jsonl"], timeout=15)
    if not raw:
        return []
    blockers: list[dict] = []
    for line in raw.splitlines():
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t.get("waiting_for"):
            blockers.append(t)
    return blockers[:limit]


def _ready_tasks(limit: int = 3) -> list[dict]:
    raw = _run(["gptodo", "ready", "--state", "backlog", "--jsonl"], timeout=15)
    if not raw:
        return []
    tasks: list[dict] = []
    for line in raw.splitlines():
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t.get("waiting_for") or t.get("wait"):
            continue
        tasks.append(t)
    return tasks[:limit]


def _journal_entries(limit: int = 5) -> list[str]:
    root = _git_root()
    if not root:
        return []
    journal_dir = root / "journal"
    if not journal_dir.is_dir():
        return []
    entries: list[Path] = []
    for day_dir in sorted(journal_dir.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        day_entries = [
            entry.relative_to(root)
            for entry in sorted(day_dir.iterdir(), reverse=True)
            if entry.is_file() and entry.suffix == ".md"
        ]
        entries.extend(day_entries)
        if len(entries) >= limit:
            break
    return [str(e) for e in entries[:limit]]


def _pr_queue_display(count: int, cap: int | None) -> str:
    if cap is not None:
        return f"{count}/{cap}" + (" ⚠ at limit" if count >= cap else "")
    return str(count)


class BobStatusProvider:
    """StatusProvider for Bob's agent workspace.

    Only contributes data inside Bob's workspace — safe to install globally.
    """

    name = "bob"

    def collect(self) -> dict[str, object]:
        if not _is_bob_workspace():
            return {}
        return {
            "bob_active_tasks": _active_tasks(3),
            "bob_pr_queue": _pr_queue(),
            "bob_services": _service_status(),
            "bob_dead_timers": _dead_timers(),
            "bob_blockers": _blockers(3),
            "bob_ready_tasks": _ready_tasks(3),
            "bob_journal_entries": _journal_entries(5),
        }

    def narrative_sections(self) -> list[str]:
        if not _is_bob_workspace():
            return []
        sections: list[str] = []

        # Active tasks
        tasks = _active_tasks(3)
        if tasks:
            lines = ["## Active Tasks"]
            for t in tasks:
                lines.append(f"- `{t['id']}` — {t.get('title', '')[:65]}")
            sections.append("\n".join(lines))

        # PR queue
        rows = _pr_queue()
        if rows:
            lines = ["## PR Queue", "| Repo | Open |", "|------|------|"]
            for row in rows:
                display = _pr_queue_display(row["count"], row["cap"])
                lines.append(f"| {row['repo']} | {display} |")
            sections.append("\n".join(lines))

        # Services
        services = _service_status()
        if services:
            lines = ["## Services"]
            lines.extend(
                f"- {svc['label']}: {svc['icon']} {svc['status']}" for svc in services
            )
            dead = _dead_timers()
            if dead:
                lines.append(f"- ⚠ {dead} dead bob-* timer(s)")
            sections.append("\n".join(lines))

        # Blockers
        blockers = _blockers(3)
        lines = ["## Top Blockers"]
        if blockers:
            for t in blockers:
                wf = str(t.get("waiting_for", "")).split("\n")[0][:70]
                since = t.get("waiting_since", "")
                since_str = f" (since {since})" if since else ""
                lines.append(f"- `{t['id']}`: {wf}{since_str}")
        else:
            lines.append("- No active blockers with waiting_for set")
        sections.append("\n".join(lines))

        # Ready next
        ready = _ready_tasks(3)
        lines = ["## Ready Next (top 3)"]
        if ready:
            for i, t in enumerate(ready, 1):
                title = str(t.get("name", t.get("id", "")))[:65]
                lines.append(f"{i}. `{t['id']}` — {title}")
        else:
            lines.append("- No ready backlog tasks found")
        sections.append("\n".join(lines))

        return sections


def make_provider() -> StatusProvider:
    """Factory function registered as the ``bob`` entry point."""
    return BobStatusProvider()  # type: ignore[return-value]
