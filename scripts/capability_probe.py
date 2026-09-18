#!/usr/bin/env python3
"""Capability probe: tell a forked agent which shared-core tiers *it* has.

Part of the shared-core convergence arc (alice#79). The companion static tool
`capability-tier-audit.py` (in an agent's own repo) answers "which capabilities
does this *script* touch?". This probe answers the runtime question a freshly
forked agent actually needs: **"which capability tiers does *my environment*
provide, and what do I lose without each?"**

The arc's premise is that the shared core must degrade gracefully across four
tiers rather than assume Alice/Bob/Gordon's privileges (see
tasks/shared-core-convergence.md and principal_notify.py):

  Tier 0  universal    filesystem + python3 — always present
  Tier 1  notify       out-of-band "notify my principal" channel (gh/pushover/telegram)
  Tier 2  service mgr   systemd / launchd — scheduled autonomous runs
  Tier 3  multi-agent   ssh to sibling agents (strictly optional)

The sharpest thing this probe surfaces is the **Tier-1 identity guard**: if the
`github` backend is selected but the authenticated `gh` login *is* the principal
(Gordon's PAT-as-Erik anti-pattern), out-of-band escalation is not merely absent
— it is *actively wrong*, because an alarm would arrive authored by the person it
is meant to alert. That is reported as `misconfigured`, not `ok`.

Dependency-light on purpose: stdlib only, so it runs on a bare fork before any
gptme install. Reuses `principal_notify.py`'s documented env-var contract without
importing it (the probe must work even when that module isn't present yet).

Usage:
    capability_probe.py                # human-readable report, exit 0
    capability_probe.py --json         # machine-readable
    capability_probe.py --strict       # exit 1 if no working out-of-band Tier-1
                                       # channel (useful as a `doctor`/setup gate)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable

# Env-var contract, kept in sync with principal_notify.py. Duplicated (not
# imported) so the probe runs on a fork that has not adopted the seam yet.
ENV_BACKENDS = "PRINCIPAL_NOTIFY_BACKENDS"
ENV_PRINCIPAL = "PRINCIPAL_NOTIFY_PRINCIPAL"
ENV_PUSHOVER = ("PUSHOVER_USER_KEY", "PUSHOVER_API_TOKEN")
ENV_TELEGRAM = ("PRINCIPAL_NOTIFY_TG_TOKEN", "PRINCIPAL_NOTIFY_TG_CHAT")

# Injectable seams so tests never touch the real PATH or spawn processes.
Which = Callable[[str], "str | None"]
Run = Callable[[list[str]], "tuple[int, str]"]


def _default_run(cmd: list[str]) -> tuple[int, str]:
    """Run a command, returning (returncode, combined-output). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return 127, ""


@dataclass
class Capability:
    tier: int
    name: str
    status: str  # "ok" | "absent" | "misconfigured"
    detail: str
    without: str  # what the agent loses when this is not ok

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Report:
    caps: list[Capability] = field(default_factory=list)

    def add(self, cap: Capability) -> None:
        self.caps.append(cap)

    @property
    def has_out_of_band(self) -> bool:
        """True iff at least one non-local Tier-1 channel is ready."""
        return any(c.tier == 1 and c.name != "local" and c.ok for c in self.caps)


def _gh_login(run: Run) -> str | None:
    """Return the authenticated gh login, or None if gh is absent/unauthed."""
    rc, out = run(["gh", "api", "user", "--jq", ".login"])
    if rc != 0:
        return None
    login = out.strip().splitlines()[0].strip() if out.strip() else ""
    return login or None


def probe_github(env: dict[str, str], which: Which, run: Run) -> Capability:
    without = "no out-of-band GitHub escalation; alarms fall back to a local file only"
    if which("gh") is None:
        return Capability(1, "github", "absent", "gh not on PATH", without)
    login = _gh_login(run)
    if login is None:
        return Capability(
            1, "github", "misconfigured", "gh present but not authenticated", without
        )
    principal = env.get(ENV_PRINCIPAL)
    if principal and login == principal:
        # Gordon's PAT-as-Erik anti-pattern: escalation would appear to come
        # from the very person it is meant to alert.
        return Capability(
            1,
            "github",
            "misconfigured",
            f"gh authenticated AS the principal ({login}) — identity guard would refuse; "
            "escalation must be attributable to the agent, not the principal",
            without,
        )
    return Capability(1, "github", "ok", f"authenticated as {login}", without)


def probe_pushover(env: dict[str, str]) -> Capability:
    without = "no Pushover push notifications to the principal's phone"
    missing = [k for k in ENV_PUSHOVER if not env.get(k)]
    if missing:
        return Capability(
            1, "pushover", "absent", f"unset: {', '.join(missing)}", without
        )
    return Capability(1, "pushover", "ok", "user key + api token set", without)


def probe_telegram(env: dict[str, str]) -> Capability:
    without = "no Telegram escalation channel"
    missing = [k for k in ENV_TELEGRAM if not env.get(k)]
    if missing:
        return Capability(
            1, "telegram", "absent", f"unset: {', '.join(missing)}", without
        )
    return Capability(1, "telegram", "ok", "bot token + chat id set", without)


def probe_service_manager(which: Which) -> Capability:
    without = "no OS-managed scheduling; autonomous runs need cron or a manual loop"
    if which("systemctl"):
        return Capability(2, "systemd", "ok", "systemctl on PATH", without)
    if which("launchctl"):
        return Capability(2, "launchd", "ok", "launchctl on PATH", without)
    return Capability(
        2, "service-manager", "absent", "no systemctl or launchctl", without
    )


def probe_ssh(which: Which) -> Capability:
    without = (
        "no multi-agent fabric; cannot reach sibling agents (fine for a solo agent)"
    )
    if which("ssh"):
        return Capability(
            3, "ssh", "ok", "ssh on PATH (sibling reachability not verified)", without
        )
    return Capability(3, "ssh", "absent", "ssh not on PATH", without)


def _selected_backends(env: dict[str, str]) -> list[str]:
    raw = env.get(ENV_BACKENDS, "local")
    return [b.strip() for b in raw.split(",") if b.strip()]


def build_report(
    env: dict[str, str] | None = None,
    which: Which | None = None,
    run: Run | None = None,
) -> Report:
    env = dict(os.environ) if env is None else env
    which = shutil.which if which is None else which
    run = _default_run if run is None else run

    r = Report()

    # Tier 0 — universal. Always present by definition; report the local-alert path.
    r.add(
        Capability(
            0,
            "local-alert",
            "ok",
            "filesystem + python3 (Tier-0 fallback always fires)",
            "nothing — this is the universal floor",
        )
    )

    # Tier 1 — only probe the backends the agent actually selected, plus always
    # report the always-on local fallback so the report shows the full ladder.
    selected = _selected_backends(env)
    probes = {
        "github": lambda: probe_github(env, which, run),
        "pushover": lambda: probe_pushover(env),
        "telegram": lambda: probe_telegram(env),
    }
    for name in selected:
        if name in probes:
            r.add(probes[name]())
        elif name == "local":
            r.add(
                Capability(
                    1,
                    "local",
                    "ok",
                    "Tier-0 fallback (in-workspace file, not out-of-band)",
                    "nothing extra — but this alone is not out-of-band escalation",
                )
            )
        # Unknown backend names are silently ignored (open registry).

    # Tier 2 + Tier 3 — environment-level, independent of notify config.
    r.add(probe_service_manager(which))
    r.add(probe_ssh(which))
    return r


_STATUS_MARK = {"ok": "ok  ", "absent": "--  ", "misconfigured": "!!  "}


def render_text(r: Report) -> str:
    lines = ["== capability probe (alice#79) =="]
    for tier in sorted({c.tier for c in r.caps}):
        lines.append(f"\nTier {tier}:")
        for c in (c for c in r.caps if c.tier == tier):
            lines.append(
                f"  [{_STATUS_MARK.get(c.status, '?   ')}] {c.name}: {c.detail}"
            )
            if not c.ok:
                lines.append(f"          without: {c.without}")
    if r.has_out_of_band:
        lines.append("\nout-of-band escalation: AVAILABLE")
    else:
        lines.append(
            "\nout-of-band escalation: NONE — alarms are local-only. A forked agent that "
            "goes dark cannot alert its principal. Configure a Tier-1 backend "
            "(PRINCIPAL_NOTIFY_BACKENDS=github|pushover|telegram)."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if no working out-of-band Tier-1 channel (setup/doctor gate)",
    )
    args = ap.parse_args(argv)

    r = build_report()

    if args.json:
        print(
            json.dumps(
                {
                    "capabilities": [asdict(c) for c in r.caps],
                    "has_out_of_band": r.has_out_of_band,
                },
                indent=2,
            )
        )
    else:
        print(render_text(r))

    if args.strict and not r.has_out_of_band:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
