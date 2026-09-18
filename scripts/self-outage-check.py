#!/usr/bin/env python3
"""Self-detect and escalate a sustained autonomous-loop auth outage.

Why this exists
---------------
An autonomous agent loop can go **credential-dead and nobody notices**. Alice's
loop was 401ing for **23 days** (2026-07-24 → 2026-08-16) and nothing on her
side caught it: the auth-guard makes the loop **exit 0** on cooldown, so systemd
saw "success" every run while zero productive work happened. The only thing that
noticed was a *sibling* agent's cross-fleet check — relying on another agent
noticing is a single point of failure.

This is a model-independent detector, deliberately pure ``python3`` + ``gh`` so
it keeps working while the LLM credential is dead. It reads the agent's session
records (which classify auth/entitlement outage outcomes) and escalates
out-of-band via a GitHub issue.

It is harness- and agent-agnostic: everything agent-specific (repo, agent name,
which failure reasons count as an outage, the re-auth command shown in the
issue) is configuration via flags or env, so a forked agent gets the alarm for
free. Upstream the *mechanism* here; keep the *policy* in a thin agent-local
wrapper (or just pass the flags from the systemd unit).

Signal
------
Over a trailing window, an outage is confirmed when ALL hold:
  - auth-outage sessions >= --min-failures    (loop 401/403ing — bad token OR
                                               org disabled subscription access)
  - productive sessions == 0                        (nothing is getting done)
  - hours since last productive session >= --min-outage-hours  (sustained, not a blip)

Escalation (--escalate)
-----------------------
Idempotent GitHub issue in --repo, deduped by title:
  - outage + no open issue   -> create issue
  - outage + open issue      -> comment update only if last update is stale
  - healthy + open issue     -> comment recovery + close
Also writes/clears ``<workspace>/state/alerts/self-outage.txt`` for surfacing in
the agent's context. If no --repo is configured the GitHub steps are skipped
(alert file + supplementary notify still run), so the detector is useful even
before an agent wires up GitHub.

Exit codes: 0 = healthy, 1 = outage confirmed, 2 = usage/error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Default classifier for "the credential is dead and a human (or self-reauth)
# must fix it out-of-band", as opposed to transient/capacity failures.
#   auth_failure   -> classic 401 (bad/expired token)
#   api_error_401  -> 401 Unauthorized
#   api_error_403  -> 403 Forbidden, incl. org-level "subscription disabled"
# 429 (quota) and 5xx (server) are transient, NOT outages — excluded on purpose.
# Override with --reasons if your records use a different vocabulary.
DEFAULT_AUTH_OUTAGE_REASONS = frozenset(
    {"auth", "auth_failure", "api_error_401", "api_error_403"}
)

# Only re-comment on an existing open issue this often, to avoid daily spam.
UPDATE_INTERVAL_HOURS = 20


@dataclass
class Config:
    """Resolved agent-specific policy for one run."""

    workspace: Path
    agent_name: str
    repo: str | None
    reasons: frozenset[str]
    reauth_cmd: str

    @property
    def records_file(self) -> Path:
        return self.workspace / "state" / "sessions" / "session-records.jsonl"

    @property
    def alert_file(self) -> Path:
        return self.workspace / "state" / "alerts" / "self-outage.txt"

    @property
    def issue_title(self) -> str:
        return f"Agent {self.agent_name} autonomous loop — auth outage (self-detected)"


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_records(records_file: Path, window_hours: float, now: datetime) -> list[dict]:
    """Load session records within the trailing window."""
    if not records_file.exists():
        return []
    cutoff = now - timedelta(hours=window_hours)
    records = []
    with records_file.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(rec.get("timestamp", ""))
            if ts is None or ts < cutoff or ts > now:
                continue
            records.append(rec)
    return records


def last_productive_ts(records_file: Path, now: datetime) -> datetime | None:
    """Timestamp of the most recent productive session, scanning all records."""
    if not records_file.exists():
        return None
    latest = None
    with records_file.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("outcome") != "productive":
                continue
            ts = _parse_ts(rec.get("timestamp", ""))
            if ts is None or ts > now:
                continue
            if latest is None or ts > latest:
                latest = ts
    return latest


def assess(
    records_file: Path,
    reasons: frozenset[str],
    window_hours: float,
    min_failures: int,
    min_outage_hours: float,
    now: datetime,
) -> dict:
    """Compute outage status. Returns a JSON-serialisable dict."""
    records = load_records(records_file, window_hours, now)
    auth_failure_records = [
        r
        for r in records
        if r.get("outcome") == "failed" and r.get("failure_reason") in reasons
    ]
    auth_failures = len(auth_failure_records)
    productive = sum(1 for r in records if r.get("outcome") == "productive")

    last_prod = last_productive_ts(records_file, now)
    if last_prod is not None:
        hours_since_prod: float | None = (now - last_prod).total_seconds() / 3600
    elif auth_failure_records:
        # No productive history: use the earliest auth failure as a proxy for
        # how long the outage has been sustained. This prevents a fresh workspace
        # with only recent failures from triggering before --min-outage-hours.
        ts_list = [_parse_ts(r.get("timestamp", "")) for r in auth_failure_records]
        earliest = min((ts for ts in ts_list if ts is not None), default=None)
        hours_since_prod = (
            (now - earliest).total_seconds() / 3600 if earliest is not None else None
        )
    else:
        hours_since_prod = None

    outage = (
        auth_failures >= min_failures
        and productive == 0
        and hours_since_prod is not None
        and hours_since_prod >= min_outage_hours
    )

    return {
        "checked_at": now.isoformat(),
        "window_hours": window_hours,
        "auth_failures_in_window": auth_failures,
        "productive_in_window": productive,
        "hours_since_last_productive": (
            round(hours_since_prod, 1) if hours_since_prod is not None else None
        ),
        "last_productive_at": last_prod.isoformat() if last_prod else None,
        "outage": outage,
    }


def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)


def find_open_issue(cfg: Config) -> dict | None:
    """Return the open self-detected outage issue, if any."""
    res = _gh(
        [
            "issue",
            "list",
            "--repo",
            cfg.repo or "",
            "--state",
            "open",
            "--author",
            "@me",
            "--search",
            cfg.issue_title,
            "--json",
            "number,title,updatedAt",
            "--limit",
            "20",
        ]
    )
    if res.returncode != 0:
        return None
    try:
        issues = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for issue in issues:
        if issue.get("title") == cfg.issue_title:
            return dict(issue)
    return None


def _issue_body(cfg: Config, status: dict) -> str:
    return (
        f"**Self-detected auth outage** — agent `{cfg.agent_name}`'s autonomous "
        f"loop is 401/403ing (bad token or org disabled subscription access) and "
        f"landing zero productive work.\n\n"
        f"- auth-outage sessions (401/403, last {status['window_hours']:.0f}h): "
        f"{status['auth_failures_in_window']}\n"
        f"- productive sessions in window: {status['productive_in_window']}\n"
        f"- hours since last productive session: "
        f"{status['hours_since_last_productive']}\n"
        f"- last productive at: {status['last_productive_at']}\n\n"
        f"The loop exits 0 on auth-cooldown, so systemd shows success — this is "
        f"why it can go silent for weeks. Fix: {cfg.reauth_cmd}. "
        f"Detector: `scripts/self-outage-check.py`.\n\n"
        f"_Auto-filed at {status['checked_at']}._"
    )


def _supplementary_notify(cfg: Config, status: dict) -> None:
    """Fan out to non-GitHub Tier-1 channels via the shared principal_notify seam.

    Additive and fully defensive: a complete no-op unless PRINCIPAL_NOTIFY_BACKENDS
    names a channel beyond github/local, and never raises — this is the alarm that
    must keep working when everything else is dead, so it cannot be taken out by a
    notification backend. GitHub is already handled by the richer issue lifecycle
    above, so it is excluded here to avoid a duplicate escalation.

    Exists so a forked agent without `gh` still gets alerted when its loop goes
    dark. Shared seam: gptme-contrib scripts/principal_notify.py (sibling of this
    file), imported only if present.
    """
    raw = os.getenv("PRINCIPAL_NOTIFY_BACKENDS", "").strip()
    extra = [
        b.strip() for b in raw.split(",") if b.strip() not in ("", "github", "local")
    ]
    if not extra:
        return
    try:
        import importlib.util

        mod_path = Path(__file__).resolve().parent / "principal_notify.py"
        if not mod_path.exists():
            return
        spec = importlib.util.spec_from_file_location("principal_notify", mod_path)
        if not (spec and spec.loader):
            return
        pn = importlib.util.module_from_spec(spec)
        # Register before exec: dataclasses under `from __future__ import
        # annotations` resolve field annotations via sys.modules[cls.__module__].
        sys.modules[spec.name] = pn
        spec.loader.exec_module(pn)
        pn_cfg = pn.Config.from_env(workspace=cfg.workspace)
        pn_cfg.backends = extra
        pn.notify_principal(
            "Self-detected auth outage",
            _issue_body(cfg, status),
            urgency="high",
            dedup_key="self-outage",
            cfg=pn_cfg,
        )
    except Exception as e:  # never let a notification channel break the alarm
        print(f"[supplementary-notify] skipped: {e}", file=sys.stderr)


def escalate(cfg: Config, status: dict, dry_run: bool, now: datetime) -> None:
    """File/update/close the outage issue and manage the local alert file."""
    cfg.alert_file.parent.mkdir(parents=True, exist_ok=True)

    if status["outage"]:
        if not dry_run:
            cfg.alert_file.write_text(
                f"SELF-OUTAGE (auth) detected at {status['checked_at']}\n"
                f"{status['auth_failures_in_window']} auth failures, "
                f"0 productive in {status['window_hours']:.0f}h; "
                f"{status['hours_since_last_productive']}h since last productive.\n"
                f"Fix: {cfg.reauth_cmd}.\n"
            )
        _supplementary_notify_pending = False
        if cfg.repo:
            existing = find_open_issue(cfg)
            if existing is None:
                if dry_run:
                    print("[dry-run] would CREATE issue:", cfg.issue_title)
                    print(_issue_body(cfg, status))
                    return
                res = _gh(
                    [
                        "issue",
                        "create",
                        "--repo",
                        cfg.repo,
                        "--title",
                        cfg.issue_title,
                        "--body",
                        _issue_body(cfg, status),
                    ]
                )
                print(res.stdout.strip() or res.stderr.strip())
                _supplementary_notify_pending = True
            else:
                updated = _parse_ts(existing.get("updatedAt", ""))
                stale = updated is None or (now - updated) >= timedelta(
                    hours=UPDATE_INTERVAL_HOURS
                )
                if not stale:
                    print(
                        f"Issue #{existing['number']} already open and fresh — no update."
                    )
                    return
                if dry_run:
                    print(f"[dry-run] would COMMENT on issue #{existing['number']}")
                    return
                res = _gh(
                    [
                        "issue",
                        "comment",
                        str(existing["number"]),
                        "--repo",
                        cfg.repo,
                        "--body",
                        _issue_body(cfg, status),
                    ]
                )
                print(res.stdout.strip() or res.stderr.strip())
        else:
            print(
                "[self-outage-check] outage confirmed; no --repo set, skipping GitHub issue."
            )
            _supplementary_notify_pending = True

        if _supplementary_notify_pending and not dry_run:
            _supplementary_notify(cfg, status)
    else:
        # Only clear and close when productive work actually resumed.
        # auth failures can age out of the window while the loop stays dead —
        # outage=False in that case does not mean recovery.
        if status["productive_in_window"] == 0:
            return
        if not dry_run and cfg.alert_file.exists():
            cfg.alert_file.unlink()
        if not cfg.repo:
            return
        existing = find_open_issue(cfg)
        if existing is None:
            return
        recovery = (
            f"**Recovered** — loop is landing productive work again as of "
            f"{status['checked_at']} (last productive "
            f"{status['hours_since_last_productive']}h ago). Auto-closing."
        )
        if dry_run:
            print(f"[dry-run] would COMMENT+CLOSE issue #{existing['number']}")
            return
        _gh(
            [
                "issue",
                "comment",
                str(existing["number"]),
                "--repo",
                cfg.repo,
                "--body",
                recovery,
            ]
        )
        _gh(["issue", "close", str(existing["number"]), "--repo", cfg.repo])
        print(f"Closed issue #{existing['number']} (recovered).")


def _resolve_config(args: argparse.Namespace) -> Config:
    workspace = Path(args.workspace or os.getenv("WORKSPACE") or Path.cwd()).resolve()
    agent_name = (
        args.agent_name or os.getenv("AGENT_NAME") or os.getenv("USER") or "agent"
    )
    repo = args.repo or os.getenv("AGENT_REPO") or None
    if args.reasons:
        reasons = frozenset(r.strip() for r in args.reasons.split(",") if r.strip())
    else:
        reasons = DEFAULT_AUTH_OUTAGE_REASONS
    return Config(
        workspace=workspace,
        agent_name=agent_name,
        repo=repo,
        reasons=reasons,
        reauth_cmd=args.reauth_cmd,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workspace", help="Agent workspace root (default $WORKSPACE or cwd)."
    )
    ap.add_argument(
        "--agent-name", help="Agent name for the issue title (default $AGENT_NAME)."
    )
    ap.add_argument(
        "--repo", help="GitHub repo owner/name for escalation (default $AGENT_REPO)."
    )
    ap.add_argument(
        "--reasons",
        help="Comma-separated failure_reason values that count as an auth outage "
        f"(default: {','.join(sorted(DEFAULT_AUTH_OUTAGE_REASONS))}).",
    )
    ap.add_argument(
        "--reauth-cmd",
        default="re-authenticate the agent's credential on its host",
        help="Remediation hint shown in the issue/alert (default: generic).",
    )
    ap.add_argument(
        "--window-hours",
        type=float,
        default=36.0,
        help="Trailing window for counting sessions (default 36).",
    )
    ap.add_argument(
        "--min-failures",
        type=int,
        default=3,
        help="Auth failures in window to confirm outage (default 3).",
    )
    ap.add_argument(
        "--min-outage-hours",
        type=float,
        default=18.0,
        help="Min hours since last productive session (default 18).",
    )
    ap.add_argument(
        "--escalate",
        action="store_true",
        help="File/update/close a GitHub issue + alert file.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="With --escalate, print intended actions only.",
    )
    ap.add_argument("--json", action="store_true", help="Emit machine-readable status.")
    args = ap.parse_args(argv)

    cfg = _resolve_config(args)
    now = datetime.now(timezone.utc)
    status = assess(
        cfg.records_file,
        cfg.reasons,
        args.window_hours,
        args.min_failures,
        args.min_outage_hours,
        now,
    )

    if args.escalate:
        escalate(cfg, status, args.dry_run, now)

    if args.json:
        print(json.dumps(status, indent=2))
    else:
        state = "OUTAGE" if status["outage"] else "healthy"
        print(
            f"[self-outage-check] {state}: "
            f"{status['auth_failures_in_window']} auth failures, "
            f"{status['productive_in_window']} productive in "
            f"{status['window_hours']:.0f}h; "
            f"{status['hours_since_last_productive']}h since last productive."
        )

    return 1 if status["outage"] else 0


if __name__ == "__main__":
    sys.exit(main())
