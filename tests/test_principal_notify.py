from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "principal_notify.py"
SPEC = importlib.util.spec_from_file_location("principal_notify", SCRIPT)
assert SPEC and SPEC.loader
principal_notify = importlib.util.module_from_spec(SPEC)
sys.modules["principal_notify"] = principal_notify
SPEC.loader.exec_module(principal_notify)

Config = principal_notify.Config
notify_principal = principal_notify.notify_principal


def cfg(workspace: Path, **overrides):
    base = dict(backends=["local"], agent_id="alice", workspace=workspace)
    base.update(overrides)
    return Config(**base)


def test_tier0_always_fires(tmp_path):
    """Local alert is written even with no Tier-1 backends, and marked degraded."""
    res = notify_principal("Outage", "body", dedup_key="self-outage", cfg=cfg(tmp_path))
    assert res.local_alert_path is not None
    assert res.local_alert_path.exists()
    assert res.degraded is True
    assert res.delivered_via == []
    content = res.local_alert_path.read_text()
    assert "Outage" in content
    assert "alice" in content


def test_tier0_fires_even_when_backend_throws(tmp_path, monkeypatch):
    """A backend that raises must not suppress the Tier-0 local alert."""

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(principal_notify.BACKENDS, "github", boom)
    res = notify_principal(
        "Outage", "b", dedup_key="k", cfg=cfg(tmp_path, backends=["github"])
    )
    assert res.local_alert_path.exists()
    assert res.degraded is True
    assert "github" in res.failures


def test_backend_delivery_clears_degraded(tmp_path, monkeypatch):
    monkeypatch.setitem(principal_notify.BACKENDS, "pushover", lambda *a, **k: "sent")
    res = notify_principal(
        "Subj", "b", cfg=cfg(tmp_path, backends=["local", "pushover"])
    )
    assert "pushover" in res.delivered_via
    assert res.degraded is False


def test_unknown_backend_recorded(tmp_path):
    res = notify_principal("s", "b", cfg=cfg(tmp_path, backends=["telepathy"]))
    assert res.failures.get("telepathy") == "unknown backend"
    assert res.degraded is True


def test_github_identity_guard_refuses_principal(tmp_path, monkeypatch):
    """The github backend must refuse when gh authenticates as the principal."""
    monkeypatch.setattr(principal_notify, "_gh_login", lambda cfg: "example-owner")
    c = cfg(
        tmp_path,
        backends=["github"],
        principal="example-owner",
        gh_repo="example-owner/example-repo",
    )
    res = notify_principal("Outage", "b", dedup_key="k", cfg=c)
    assert res.degraded is True
    assert "github" in res.failures
    assert "principal" in res.failures["github"].lower()
    # Tier-0 still fired — the alarm is not lost, just downgraded.
    assert res.local_alert_path.exists()


def test_github_identity_guard_refuses_when_principal_unset(tmp_path, monkeypatch):
    """The github backend must fail closed when PRINCIPAL_NOTIFY_PRINCIPAL is not set."""
    monkeypatch.setattr(principal_notify, "_gh_login", lambda cfg: "example-agent")
    c = cfg(
        tmp_path,
        backends=["github"],
        gh_repo="example-owner/example-repo",
        # principal deliberately omitted
    )
    res = notify_principal("Outage", "b", dedup_key="k", cfg=c)
    assert res.degraded is True
    assert "github" in res.failures


def test_github_backend_files_issue_as_agent(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(principal_notify, "_gh_login", lambda cfg: "example-agent")

    def fake_run(args, **kwargs):
        calls["args"] = args

        class R:
            returncode = 0
            stdout = "https://github.com/example-owner/example-repo/issues/99"
            stderr = ""

        return R()

    monkeypatch.setattr(principal_notify.subprocess, "run", fake_run)
    c = cfg(
        tmp_path,
        backends=["github"],
        principal="example-owner",
        gh_repo="example-owner/example-repo",
    )
    res = notify_principal("Outage", "b", dedup_key="self-outage", cfg=c)
    assert "github" in res.delivered_via
    assert res.degraded is False
    # attribution: the agent id appears in the issue body
    body_arg = calls["args"][calls["args"].index("--body") + 1]
    assert "alice" in body_arg


def test_clear_local_alert(tmp_path):
    c = cfg(tmp_path)
    notify_principal("s", "b", dedup_key="recover-me", cfg=c)
    assert principal_notify.clear_local_alert(c, "recover-me") is True
    # idempotent: clearing again is a no-op returning False
    assert principal_notify.clear_local_alert(c, "recover-me") is False


def test_cli_dry_run_writes_local_only(tmp_path, capsys):
    rc = principal_notify.main(
        [
            "--subject",
            "Test",
            "--body",
            "hello",
            "--dedup-key",
            "cli-test",
            "--workspace",
            str(tmp_path),
            "--dry-run",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0  # dry-run requests no Tier-1, so not counted as degraded failure
    assert Path(out["local_alert_path"]).exists()
    assert out["delivered_via"] == []


def test_from_env_parses_backends(monkeypatch, tmp_path):
    env = {
        "PRINCIPAL_NOTIFY_BACKENDS": "github, pushover",
        "PRINCIPAL_NOTIFY_AGENT_ID": "example-agent",
        "PRINCIPAL_NOTIFY_PRINCIPAL": "example-owner",
    }
    c = Config.from_env(workspace=tmp_path, env=env)
    assert c.backends == ["github", "pushover"]
    assert c.agent_id == "example-agent"
    assert c.principal == "example-owner"
