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
  `degraded=True`. **A local alert file is storage, not a notification** — it
  only helps if something later reads the workspace. Callers that mean "page the
  operator" must check `result.degraded` (or the CLI exit code), never just
  "did a file appear".
- **Tier 1 — pluggable channel.** `github` (gh), `pushover`, `telegram`, `ntfy`
  today; the registry is open. Selected via config/env, so a core script calls
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
    PRINCIPAL_NOTIFY_AGENT_ID   identity stamp, e.g. "my-agent"
    PRINCIPAL_NOTIFY_PRINCIPAL  principal's identity, e.g. "example-owner" (github guard)
    PRINCIPAL_NOTIFY_GH_REPO    repo for github backend, e.g. "example-owner/example-repo"
    PUSHOVER_USER_KEY / PUSHOVER_API_TOKEN   pushover backend
    PRINCIPAL_NOTIFY_TG_TOKEN / PRINCIPAL_NOTIFY_TG_CHAT   telegram backend
    PRINCIPAL_NOTIFY_NTFY_URL   self-hosted ntfy server base, e.g. "http://kuma-host:2586"
    PRINCIPAL_NOTIFY_NTFY_TOPIC ntfy topic to publish to
    PRINCIPAL_NOTIFY_NTFY_TOKEN optional bearer token for a protected topic
    PRINCIPAL_NOTIFY_NTFY_ENV_FILE  optional KEY=VALUE file (mode 600, outside git)
                                    holding the three NTFY vars; already-set
                                    environment values win and are never overridden

The ntfy backend's success criterion is the *server acknowledgement*: HTTP 200
plus a JSON body carrying a message `id`. ntfy has no read receipt, so this must
not be reported as "the operator saw it" — only as "the server accepted it, and
one durable receipt was written".
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    @property
    def operator_notified(self) -> bool:
        """True only when a Tier-1 channel accepted the alert.

        Kept separate from `local_alert_path` on purpose: writing the local file
        always succeeds, so treating it as delivery is how an outage goes
        unnoticed (gptme/gptme-contrib#2171).
        """
        return bool(self.delivered_via)

    def as_dict(self) -> dict:
        return {
            "delivered_via": self.delivered_via,
            "failures": self.failures,
            "local_alert_path": str(self.local_alert_path)
            if self.local_alert_path
            else None,
            "degraded": self.degraded,
            "operator_notified": self.operator_notified,
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
    if not cfg.principal:
        raise BackendError(
            "PRINCIPAL_NOTIFY_PRINCIPAL must be set when using the github backend"
        )
    login = _gh_login(cfg)
    # Identity rule: never escalate an alarm that appears to come from the person
    # it is meant to alert (Gordon's PAT-as-Erik anti-pattern).
    if login and login.lower() == cfg.principal.lower():
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
    _TELEGRAM_MAX = 4096
    msg = f"[{cfg.agent_id}] {subject}\n\n{body}"
    if len(msg) > _TELEGRAM_MAX:
        msg = msg[: _TELEGRAM_MAX - 3] + "..."
    data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise BackendError(f"telegram HTTP {resp.status}")
    except urllib.error.URLError as e:
        raise BackendError(f"telegram request failed: {e}") from e
    return "sent"


# --- ntfy backend ----------------------------------------------------------

# ntfy priorities are 1..5: 1 min, 2 low, 3 default, 4 high, 5 max/urgent.
# Default (3) is wrong for an escalation: an alarm that arrives at the same
# volume as chatter is easy to miss, so map the caller's urgency explicitly.
_NTFY_PRIORITY = {"low": 2, "normal": 3, "high": 4, "critical": 5}
_NTFY_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Receipts live in the workspace so a later reader can prove a page was
# accepted. Append-only, one JSON object per accepted send.
_NTFY_ACK_LEDGER = Path("state") / "notify-acks.jsonl"


def _load_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE env file. Missing/unreadable is an error, not a silent skip.

    Returns only the keys it found; callers merge with the process environment so
    an explicit env value is never overridden by a file.
    """
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        # The path itself can carry a username; report only the failure class.
        raise BackendError(f"ntfy env file unreadable ({type(exc).__name__})") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, raw = line.partition("=")
        values[name.strip()] = raw.strip().strip('"').strip("'")
    return values


def _ntfy_settings(cfg: Config) -> tuple[str, str, str]:
    """Return (url, topic, token) for the ntfy backend.

    Precedence: process/config env, then `PRINCIPAL_NOTIFY_NTFY_ENV_FILE`. Values
    are validated before any request so a typo fails fast instead of silently
    paging nobody.
    """
    env = dict(cfg.env)
    env_file = env.get("PRINCIPAL_NOTIFY_NTFY_ENV_FILE", "").strip()
    if env_file:
        for name, value in _load_env_file(env_file).items():
            if name not in env:
                env[name] = value

    url = env.get("PRINCIPAL_NOTIFY_NTFY_URL", "").strip().rstrip("/")
    topic = env.get("PRINCIPAL_NOTIFY_NTFY_TOPIC", "").strip()
    token = env.get("PRINCIPAL_NOTIFY_NTFY_TOKEN", "").strip()

    if not url:
        raise BackendError("PRINCIPAL_NOTIFY_NTFY_URL not set")
    if not url.startswith(("http://", "https://")):
        raise BackendError(
            "PRINCIPAL_NOTIFY_NTFY_URL must start with http:// or https://"
        )
    if not topic:
        raise BackendError("PRINCIPAL_NOTIFY_NTFY_TOPIC not set")
    if not _NTFY_TOPIC_RE.fullmatch(topic):
        raise BackendError(
            "PRINCIPAL_NOTIFY_NTFY_TOPIC must be 1-64 chars of [A-Za-z0-9_-]"
        )
    return url, topic, token


def _record_ntfy_ack(
    cfg: Config, dedup_key: str, subject: str, urgency: str, message_id: str
) -> None:
    """Append a durable receipt for an accepted page.

    Best-effort and never fatal: a receipt failure must not downgrade a page the
    server already accepted. The URL, topic and token are deliberately absent —
    the topic is a capability on many self-hosted setups.
    """
    ledger = cfg.workspace / _NTFY_ACK_LEDGER
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": "ntfy",
        "agent_id": cfg.agent_id,
        "dedup_key": dedup_key,
        "subject": subject,
        "urgency": urgency,
        "message_id": message_id,
    }
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        print(
            f"principal_notify: ntfy page accepted but receipt not written: {exc}",
            file=sys.stderr,
        )


def backend_ntfy(cfg: Config, subject: str, body: str, dedup_key: str, urgency: str):
    """Publish to a self-hosted ntfy server; success requires a server ack.

    Uses ntfy's JSON publish form (`POST <base>` with a `topic` field) so a topic
    is never spliced into the URL and non-ASCII titles are encoded by JSON.
    """
    url, topic, token = _ntfy_settings(cfg)
    payload = {
        "topic": topic,
        "title": f"[{cfg.agent_id}] {subject}",
        "message": body,
        "priority": _NTFY_PRIORITY.get(urgency, 3),
    }
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(
                json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
            )
        except (ValueError, OSError):
            detail = ""
        suffix = f": {detail[:120]}" if detail else ""
        raise BackendError(f"ntfy HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        # Never interpolate the request: the exception can carry the URL, and a
        # self-hosted ntfy URL may embed a capability token.
        raise BackendError(f"ntfy request failed: {type(exc.reason).__name__}") from exc
    except (http.client.HTTPException, OSError, ValueError) as exc:
        # InvalidURL and request/header validation failures may include secrets.
        raise BackendError(f"ntfy request failed: {type(exc).__name__}") from exc

    try:
        ack = json.loads(raw)
    except ValueError as exc:
        raise BackendError("ntfy returned a non-JSON body; not acknowledged") from exc
    message_id = ack.get("id") if isinstance(ack, dict) else None
    if not message_id:
        raise BackendError(
            "ntfy accepted the request but returned no message id; not acknowledged"
        )

    _record_ntfy_ack(cfg, dedup_key, subject, urgency, str(message_id))
    return f"accepted id={message_id}"


BACKENDS = {
    "github": backend_github,
    "pushover": backend_pushover,
    "telegram": backend_telegram,
    "ntfy": backend_ntfy,
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
