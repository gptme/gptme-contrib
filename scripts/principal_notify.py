#!/usr/bin/env python3
"""Notify-my-principal: a pluggable notification seam for shared-core scripts.

Part of the shared-core convergence arc (alice#79). Core scripts like
`self-outage-check.py` need to *escalate out-of-band to the human who owns the
agent* — but "escalate" was hardwired to `gh issue create`. For any forked agent
without `gh`, that alarm silently does nothing, which is the exact failure the
alarm exists to prevent. This module makes "notify my principal" an **interface**
with a Tier-0 fallback that always fires.

Design (from tasks/shared-core-convergence.md):

- **Tier 0 — universal.** Always writes a local alert file that `context.sh`
  surfaces. Requires only the filesystem + python3. Never a silent no-op: if no
  Tier-1 backend delivers, the local alert still exists and the result is marked
  `degraded=True`.
- **Tier 1 — pluggable channel.** `github` (gh), `pushover`, `telegram` today;
  the registry is open. Selected via config/env, so a core script calls
  `notify_principal(...)` without knowing which channel a given agent uses.

**Identity rule.** An escalation must be attributable to the *agent*, never to
the principal. The `github` backend refuses to act if the authenticated `gh`
login *is* the principal (Gordon's PAT-as-Erik anti-pattern) — it degrades to
Tier-0 rather than filing an alarm that appears to come from the person it is
meant to alert.

Dependency-light on purpose: only stdlib (subprocess, urllib). This module has
to run when the model/auth is dead, so it must not import gptme or `requests`.

CLI:
    principal_notify.py --subject "Auth outage" --body "..." --dedup-key self-outage \\
        [--urgency high] [--workspace /home/alice/alice] [--dry-run]

Config (env; a core script can also pass a Config explicitly):
    PRINCIPAL_NOTIFY_BACKENDS   comma list, e.g. "github,pushover" (default "local")
    PRINCIPAL_NOTIFY_AGENT_ID   identity stamp, e.g. "alice"
    PRINCIPAL_NOTIFY_PRINCIPAL  principal's identity, e.g. "ErikBjare" (github guard)
    PRINCIPAL_NOTIFY_GH_REPO    repo for github backend, e.g. "ErikBjare/alice"
    PUSHOVER_USER_KEY / PUSHOVER_API_TOKEN   pushover backend
    PRINCIPAL_NOTIFY_TG_TOKEN / PRINCIPAL_NOTIFY_TG_CHAT   telegram backend
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# --- Config ----------------------------------------------------------------


@dataclass
class Config:
    backends: list[str]
    agent_id: str
    principal: str | None = None
    gh_repo: str | None = None
    workspace: Path = field(default_factory=lambda: Path.cwd())
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))

    @classmethod
    def from_env(
        cls, workspace: Path | None = None, env: dict[str, str] | None = None
    ) -> Config:
        env = dict(env if env is not None else os.environ)
        raw = env.get("PRINCIPAL_NOTIFY_BACKENDS", "local").strip()
        backends = [b.strip() for b in raw.split(",") if b.strip()] or ["local"]
        ws = workspace or Path(env.get("ALICE_WORKSPACE", Path.cwd()))
        return cls(
            backends=backends,
            agent_id=env.get("PRINCIPAL_NOTIFY_AGENT_ID", "agent"),
            principal=env.get("PRINCIPAL_NOTIFY_PRINCIPAL") or None,
            gh_repo=env.get("PRINCIPAL_NOTIFY_GH_REPO") or None,
            workspace=Path(ws),
            env=env,
        )


@dataclass
class NotifyResult:
    delivered_via: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    local_alert_path: Path | None = None
    degraded: bool = True  # True while no Tier-1 backend has delivered

    def as_dict(self) -> dict:
        return {
            "delivered_via": self.delivered_via,
            "failures": self.failures,
            "local_alert_path": str(self.local_alert_path)
            if self.local_alert_path
            else None,
            "degraded": self.degraded,
        }


# --- Tier 0: local alert file (always) -------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-") or "notify"


def write_local_alert(
    cfg: Config, subject: str, body: str, dedup_key: str, urgency: str
) -> Path:
    """Tier-0 fallback. Always succeeds given a writable workspace.

    context.sh surfaces files under state/alerts/. self-outage-check.py already
    uses state/alerts/self-outage.txt; this generalises that path.
    """
    alert_dir = cfg.workspace / "state" / "alerts"
    alert_dir.mkdir(parents=True, exist_ok=True)
    path = alert_dir / f"{_slug(dedup_key)}.txt"
    path.write_text(
        f"[{urgency.upper()}] {subject}\nfrom: {cfg.agent_id}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def clear_local_alert(cfg: Config, dedup_key: str) -> bool:
    """Remove a previously written local alert (recovery). Returns True if removed."""
    path = cfg.workspace / "state" / "alerts" / f"{_slug(dedup_key)}.txt"
    if path.exists():
        path.unlink()
        return True
    return False


# --- Tier 1 backends -------------------------------------------------------


class BackendError(Exception):
    pass


def _gh_login(cfg: Config) -> str | None:
    try:
        res = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        raise BackendError(f"gh unavailable: {e}") from e
    if res.returncode != 0:
        raise BackendError(f"gh api user failed: {res.stderr.strip()}")
    return res.stdout.strip() or None


def backend_github(cfg: Config, subject: str, body: str, dedup_key: str, urgency: str):
    """File a GitHub issue as the agent. Refuses if authenticated as the principal."""
    if not cfg.gh_repo:
        raise BackendError("PRINCIPAL_NOTIFY_GH_REPO not set")
    login = _gh_login(cfg)
    # Identity rule: never escalate an alarm that appears to come from the person
    # it is meant to alert (Gordon's PAT-as-Erik anti-pattern).
    if cfg.principal and login and login.lower() == cfg.principal.lower():
        raise BackendError(
            f"refusing: gh authenticates as principal '{login}' — an escalation "
            "must be attributable to the agent, not the principal"
        )
    res = subprocess.run(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            cfg.gh_repo,
            "--title",
            f"[{urgency}] {subject}",
            "--body",
            f"{body}\n\n— escalated by {cfg.agent_id} (dedup: {dedup_key})",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode != 0:
        raise BackendError(f"gh issue create failed: {res.stderr.strip()}")
    return res.stdout.strip()


def backend_pushover(
    cfg: Config, subject: str, body: str, dedup_key: str, urgency: str
):
    user = cfg.env.get("PUSHOVER_USER_KEY")
    token = cfg.env.get("PUSHOVER_API_TOKEN")
    if not (user and token):
        raise BackendError("PUSHOVER_USER_KEY / PUSHOVER_API_TOKEN not set")
    priority = {"low": -1, "normal": 0, "high": 1, "critical": 2}.get(urgency, 0)
    data = urllib.parse.urlencode(
        {
            "token": token,
            "user": user,
            "title": f"[{cfg.agent_id}] {subject}",
            "message": body,
            "priority": priority,
        }
    ).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise BackendError(f"pushover HTTP {resp.status}")
    except urllib.error.URLError as e:
        raise BackendError(f"pushover request failed: {e}") from e
    return "sent"


def backend_telegram(
    cfg: Config, subject: str, body: str, dedup_key: str, urgency: str
):
    tg_token = cfg.env.get("PRINCIPAL_NOTIFY_TG_TOKEN")
    chat = cfg.env.get("PRINCIPAL_NOTIFY_TG_CHAT")
    if not (tg_token and chat):
        raise BackendError("PRINCIPAL_NOTIFY_TG_TOKEN / _TG_CHAT not set")
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": f"[{cfg.agent_id}] {subject}\n\n{body}"}
    ).encode()
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise BackendError(f"telegram HTTP {resp.status}")
    except urllib.error.URLError as e:
        raise BackendError(f"telegram request failed: {e}") from e
    return "sent"


BACKENDS = {
    "github": backend_github,
    "pushover": backend_pushover,
    "telegram": backend_telegram,
}


# --- Public API ------------------------------------------------------------


def notify_principal(
    subject: str,
    body: str,
    *,
    urgency: str = "normal",
    dedup_key: str | None = None,
    cfg: Config | None = None,
    workspace: Path | None = None,
) -> NotifyResult:
    """Notify the agent's principal across configured channels.

    Tier-0 (local alert file) always fires. Each configured Tier-1 backend is
    attempted; failures are recorded but never raised — a dead channel must not
    take out the others or suppress the local alert. `degraded` is True unless at
    least one Tier-1 backend delivered.
    """
    cfg = cfg or Config.from_env(workspace=workspace)
    dedup_key = dedup_key or _slug(subject)
    result = NotifyResult()

    # Tier 0 — always, first, so the alert exists even if everything below throws.
    result.local_alert_path = write_local_alert(cfg, subject, body, dedup_key, urgency)

    for name in cfg.backends:
        if name == "local":
            continue  # Tier-0 already handled
        fn = BACKENDS.get(name)
        if fn is None:
            result.failures[name] = "unknown backend"
            continue
        try:
            fn(cfg, subject, body, dedup_key, urgency)
            result.delivered_via.append(name)
            result.degraded = False
        except BackendError as e:
            result.failures[name] = str(e)
        except Exception as e:  # a backend bug must not kill the alarm
            result.failures[name] = f"unexpected: {e}"

    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--urgency", default="normal")
    ap.add_argument("--dedup-key", default=None)
    ap.add_argument("--workspace", default=None)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Write only the Tier-0 local alert; skip Tier-1 backends.",
    )
    args = ap.parse_args(argv)

    ws = Path(args.workspace) if args.workspace else None
    cfg = Config.from_env(workspace=ws)
    if args.dry_run:
        cfg.backends = ["local"]
    result = notify_principal(
        args.subject,
        args.body,
        urgency=args.urgency,
        dedup_key=args.dedup_key,
        cfg=cfg,
    )
    print(json.dumps(result.as_dict(), indent=2))
    # Exit non-zero if we could not reach the principal via any Tier-1 channel
    # AND Tier-1 was requested — lets a caller detect a degraded escalation.
    requested_tier1 = [b for b in cfg.backends if b != "local"]
    return 1 if (requested_tier1 and result.degraded) else 0


if __name__ == "__main__":
    sys.exit(main())
