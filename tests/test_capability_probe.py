"""Tests for capability_probe.py — the runtime shared-core tier probe (alice#79).

No network, no real subprocess: the `which` PATH lookup and the `run` subprocess
are both injected, so every tier is exercised deterministically.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODPATH = Path(__file__).resolve().parent.parent / "scripts" / "capability_probe.py"
_spec = importlib.util.spec_from_file_location("capability_probe", _MODPATH)
assert _spec and _spec.loader
cp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cp  # dataclass introspection needs the module registered
_spec.loader.exec_module(cp)


def which_with(*present: str):
    have = set(present)
    return lambda name: f"/usr/bin/{name}" if name in have else None


def run_none(_cmd):  # never authed
    return 127, ""


def run_gh_login(login: str):
    def _run(cmd):
        if cmd[:2] == ["gh", "api"]:
            return 0, login + "\n"
        return 127, ""

    return _run


# --- Tier 0 ---------------------------------------------------------------


def test_tier0_ok_when_workspace_writable():
    r = cp.build_report(
        env={}, which=which_with(), run=run_none, can_write=lambda: True
    )
    t0 = [c for c in r.caps if c.tier == 0]
    assert len(t0) == 1
    assert t0[0].ok  # universal floor is ok when the FS accepts writes


def test_tier0_misconfigured_when_workspace_not_writable():
    # A read-only or full workspace means even the local-alert file cannot be
    # written, so the Tier-0 floor must not report a false ok.
    r = cp.build_report(
        env={}, which=which_with(), run=run_none, can_write=lambda: False
    )
    t0 = next(c for c in r.caps if c.tier == 0)
    assert t0.status == "misconfigured"


# --- Tier 1: github identity guard (the sharp case) ------------------------


def test_github_ok_when_login_differs_from_principal():
    env = {cp.ENV_BACKENDS: "github", cp.ENV_PRINCIPAL: "example-principal"}
    r = cp.build_report(
        env=env, which=which_with("gh"), run=run_gh_login("some-agent-bot")
    )
    gh = next(c for c in r.caps if c.name == "github")
    assert gh.status == "ok"
    assert r.has_out_of_band


def test_github_misconfigured_when_authed_as_principal():
    # PAT-as-principal anti-pattern: gh login == principal.
    env = {cp.ENV_BACKENDS: "github", cp.ENV_PRINCIPAL: "example-principal"}
    r = cp.build_report(
        env=env, which=which_with("gh"), run=run_gh_login("example-principal")
    )
    gh = next(c for c in r.caps if c.name == "github")
    assert gh.status == "misconfigured"
    assert "identity guard" in gh.detail
    assert not r.has_out_of_band  # a misconfigured channel is not out-of-band


def test_github_identity_guard_is_case_insensitive():
    # GitHub logins are case-insensitive, so a case-only difference is still
    # the principal's own PAT and must be caught.
    env = {cp.ENV_BACKENDS: "github", cp.ENV_PRINCIPAL: "example-principal"}
    r = cp.build_report(
        env=env, which=which_with("gh"), run=run_gh_login("Example-Principal")
    )
    gh = next(c for c in r.caps if c.name == "github")
    assert gh.status == "misconfigured"
    assert not r.has_out_of_band


def test_github_misconfigured_when_principal_unset():
    # With no principal declared the identity guard cannot run, so a present
    # PAT must not be reported as a safe out-of-band channel.
    env = {cp.ENV_BACKENDS: "github"}
    r = cp.build_report(
        env=env, which=which_with("gh"), run=run_gh_login("some-agent-bot")
    )
    gh = next(c for c in r.caps if c.name == "github")
    assert gh.status == "misconfigured"
    assert cp.ENV_PRINCIPAL in gh.detail
    assert not r.has_out_of_band


def test_github_absent_when_no_gh():
    env = {cp.ENV_BACKENDS: "github"}
    r = cp.build_report(env=env, which=which_with(), run=run_none)
    gh = next(c for c in r.caps if c.name == "github")
    assert gh.status == "absent"


def test_github_misconfigured_when_unauthenticated():
    env = {cp.ENV_BACKENDS: "github"}
    r = cp.build_report(env=env, which=which_with("gh"), run=run_none)
    gh = next(c for c in r.caps if c.name == "github")
    assert gh.status == "misconfigured"


# --- Tier 1: other backends + default -------------------------------------


def test_pushover_ok_when_both_keys_set():
    env = {
        cp.ENV_BACKENDS: "pushover",
        "PUSHOVER_USER_KEY": "u",
        "PUSHOVER_API_TOKEN": "t",
    }
    r = cp.build_report(env=env, which=which_with(), run=run_none)
    assert next(c for c in r.caps if c.name == "pushover").ok
    assert r.has_out_of_band


def test_pushover_absent_when_partial():
    env = {cp.ENV_BACKENDS: "pushover", "PUSHOVER_USER_KEY": "u"}
    r = cp.build_report(env=env, which=which_with(), run=run_none)
    assert next(c for c in r.caps if c.name == "pushover").status == "absent"


def test_pushover_blank_value_counts_as_unset():
    # A whitespace-only value would never authenticate; it must not pass strict.
    env = {
        cp.ENV_BACKENDS: "pushover",
        "PUSHOVER_USER_KEY": "u",
        "PUSHOVER_API_TOKEN": "   ",
    }
    r = cp.build_report(env=env, which=which_with(), run=run_none)
    assert next(c for c in r.caps if c.name == "pushover").status == "absent"
    assert not r.has_out_of_band


def test_default_backend_is_local_only_not_out_of_band():
    # No PRINCIPAL_NOTIFY_BACKENDS => "local" => not out-of-band.
    r = cp.build_report(env={}, which=which_with(), run=run_none)
    assert next(c for c in r.caps if c.name == "local").ok
    assert not r.has_out_of_band


def test_unknown_backend_ignored():
    env = {
        cp.ENV_BACKENDS: "carrier-pigeon,pushover",
        "PUSHOVER_USER_KEY": "u",
        "PUSHOVER_API_TOKEN": "t",
    }
    r = cp.build_report(env=env, which=which_with(), run=run_none)
    names = {c.name for c in r.caps if c.tier == 1}
    assert "carrier-pigeon" not in names
    assert "pushover" in names


# --- Tier 2 / Tier 3 -------------------------------------------------------


def run_systemctl_ok(cmd):
    # The --user manager answers (rc 0 = running; any non-127 proves reachable).
    if cmd[:2] == ["systemctl", "--user"]:
        return 0, "running\n"
    return 127, ""


def test_service_manager_prefers_systemd_when_user_manager_reachable():
    r = cp.build_report(
        env={},
        which=which_with("systemctl", "launchctl"),
        run=run_systemctl_ok,
    )
    sm = next(c for c in r.caps if c.tier == 2)
    assert sm.name == "systemd" and sm.ok


def test_service_manager_misconfigured_when_user_manager_unreachable():
    # systemctl on PATH but no session bus / user manager (containers, WSL):
    # `systemctl --user` fails to spawn, so Tier 2 is not actually usable.
    r = cp.build_report(env={}, which=which_with("systemctl"), run=run_none)
    sm = next(c for c in r.caps if c.tier == 2)
    assert sm.name == "systemd" and sm.status == "misconfigured"


def test_service_manager_absent():
    r = cp.build_report(env={}, which=which_with(), run=run_none)
    assert next(c for c in r.caps if c.tier == 2).status == "absent"


def test_ssh_tier3_optional():
    r = cp.build_report(env={}, which=which_with("ssh"), run=run_none)
    assert next(c for c in r.caps if c.tier == 3).ok


# --- CLI / strict gate -----------------------------------------------------


def test_strict_exits_nonzero_without_out_of_band(monkeypatch):
    monkeypatch.setattr(cp, "build_report", lambda: cp.Report(caps=[]))
    assert cp.main(["--strict"]) == 1


def test_strict_exits_zero_with_out_of_band(monkeypatch):
    good = cp.Report(caps=[cp.Capability(1, "pushover", "ok", "", "")])
    monkeypatch.setattr(cp, "build_report", lambda: good)
    assert cp.main(["--strict"]) == 0


def test_json_output_smoke(capsys, monkeypatch):
    monkeypatch.setattr(
        cp,
        "build_report",
        lambda: cp.Report(caps=[cp.Capability(0, "x", "ok", "d", "w")]),
    )
    assert cp.main(["--json"]) == 0
    import json as _json

    payload = _json.loads(capsys.readouterr().out)
    assert payload["capabilities"][0]["name"] == "x"
    assert "has_out_of_band" in payload
