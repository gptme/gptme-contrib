from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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


# --- ntfy backend ----------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ntfy_env(**overrides):
    env = {
        "PRINCIPAL_NOTIFY_NTFY_URL": "http://kuma.example:2586",
        "PRINCIPAL_NOTIFY_NTFY_TOPIC": "erik-homelab",
    }
    env.update(overrides)
    return env


def _capture_urlopen(monkeypatch, payload=None, error=None):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.data.decode())
        if error is not None:
            raise error
        return _FakeResponse(payload or {"id": "msg-1", "event": "message"})

    monkeypatch.setattr(principal_notify.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_ntfy_publishes_with_priority_and_records_receipt(tmp_path, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    c = cfg(tmp_path, backends=["ntfy"], env=_ntfy_env())
    res = notify_principal(
        "Auth outage", "no model calls", urgency="critical", dedup_key="auth", cfg=c
    )
    assert "ntfy" in res.delivered_via
    assert res.degraded is False
    assert res.operator_notified is True
    assert seen["body"]["topic"] == "erik-homelab"
    assert seen["body"]["priority"] == 5
    assert "[alice]" in seen["body"]["title"]

    ledger = tmp_path / "state" / "notify-acks.jsonl"
    entry = json.loads(ledger.read_text().splitlines()[0])
    assert entry["message_id"] == "msg-1"
    assert entry["dedup_key"] == "auth"
    # The secret-lite surface must not reach durable state.
    raw = ledger.read_text()
    assert "kuma.example" not in raw
    assert "erik-homelab" not in raw


@pytest.mark.parametrize(
    "urgency,expected",
    [("low", 2), ("normal", 3), ("high", 4), ("critical", 5), ("bogus", 3)],
)
def test_ntfy_urgency_mapping(tmp_path, monkeypatch, urgency, expected):
    seen = _capture_urlopen(monkeypatch)
    notify_principal(
        "s", "b", urgency=urgency, cfg=cfg(tmp_path, backends=["ntfy"], env=_ntfy_env())
    )
    assert seen["body"]["priority"] == expected


def test_ntfy_missing_topic_fails_closed(tmp_path, monkeypatch):
    # No request may be attempted when the config is incomplete.
    def explode(*a, **k):
        raise AssertionError("urlopen must not be called")

    monkeypatch.setattr(principal_notify.urllib.request, "urlopen", explode)
    env = _ntfy_env()
    env.pop("PRINCIPAL_NOTIFY_NTFY_TOPIC")
    res = notify_principal("s", "b", cfg=cfg(tmp_path, backends=["ntfy"], env=env))
    assert res.degraded is True
    assert "NTFY_TOPIC" in res.failures["ntfy"]
    assert res.local_alert_path.exists()


def test_ntfy_invalid_topic_rejected(tmp_path, monkeypatch):
    _capture_urlopen(monkeypatch)
    res = notify_principal(
        "s",
        "b",
        cfg=cfg(
            tmp_path,
            backends=["ntfy"],
            env=_ntfy_env(PRINCIPAL_NOTIFY_NTFY_TOPIC="bad topic/../x"),
        ),
    )
    assert res.degraded is True
    assert "1-64 chars" in res.failures["ntfy"]


def test_ntfy_non_http_url_rejected(tmp_path):
    res = notify_principal(
        "s",
        "b",
        cfg=cfg(
            tmp_path,
            backends=["ntfy"],
            env=_ntfy_env(PRINCIPAL_NOTIFY_NTFY_URL="ftp://x"),
        ),
    )
    assert "http://" in res.failures["ntfy"]


def test_ntfy_http_error_does_not_leak_url_or_token(tmp_path, monkeypatch):
    import email.message
    import urllib.error

    error = urllib.error.HTTPError(
        "http://kuma.example:2586/?token=supersecret",
        403,
        "Forbidden",
        email.message.Message(),
        __import__("io").BytesIO(b'{"error":"forbidden"}'),
    )
    _capture_urlopen(monkeypatch, error=error)
    res = notify_principal(
        "s",
        "b",
        cfg=cfg(
            tmp_path,
            backends=["ntfy"],
            env=_ntfy_env(
                PRINCIPAL_NOTIFY_NTFY_URL="http://kuma.example:2586/?token=supersecret",
                PRINCIPAL_NOTIFY_NTFY_TOKEN="tk_secret_value",
            ),
        ),
    )
    assert res.degraded is True
    failure = res.failures["ntfy"]
    assert "ntfy HTTP 403" in failure
    assert "supersecret" not in failure
    assert "tk_secret_value" not in failure


def test_ntfy_network_error_does_not_leak_url(tmp_path, monkeypatch):
    import urllib.error

    _capture_urlopen(monkeypatch, error=urllib.error.URLError("connection refused"))
    res = notify_principal(
        "s",
        "b",
        cfg=cfg(
            tmp_path,
            backends=["ntfy"],
            env=_ntfy_env(PRINCIPAL_NOTIFY_NTFY_URL="http://secret-host:2586"),
        ),
    )
    assert "secret-host" not in res.failures["ntfy"]


def test_ntfy_without_message_id_is_not_delivery(tmp_path, monkeypatch):
    """HTTP 200 alone is not an ack; the server must name the accepted message."""
    _capture_urlopen(monkeypatch, payload={"event": "message"})
    res = notify_principal(
        "s", "b", cfg=cfg(tmp_path, backends=["ntfy"], env=_ntfy_env())
    )
    assert res.degraded is True
    assert "no message id" in res.failures["ntfy"]
    assert not (tmp_path / "state" / "notify-acks.jsonl").exists()


def test_ntfy_env_file_fills_only_missing_env_values(tmp_path, monkeypatch):
    env_file = tmp_path / "ntfy.env"
    env_file.write_text(
        'PRINCIPAL_NOTIFY_NTFY_URL="http://from-file:2586"\n'
        'PRINCIPAL_NOTIFY_NTFY_TOPIC="file-topic"\n'
    )
    seen = _capture_urlopen(monkeypatch)
    # Env wins for the topic; the file supplies the URL.
    c = cfg(
        tmp_path,
        backends=["ntfy"],
        env={
            "PRINCIPAL_NOTIFY_NTFY_TOPIC": "env-topic",
            "PRINCIPAL_NOTIFY_NTFY_ENV_FILE": str(env_file),
        },
    )
    res = notify_principal("s", "b", cfg=c)
    assert res.degraded is False
    assert seen["body"]["topic"] == "env-topic"
    assert seen["url"] == "http://from-file:2586"


def test_ntfy_env_file_unreadable_is_a_failure(tmp_path, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("urlopen must not be called")

    monkeypatch.setattr(principal_notify.urllib.request, "urlopen", explode)
    res = notify_principal(
        "s",
        "b",
        cfg=cfg(
            tmp_path,
            backends=["ntfy"],
            env={"PRINCIPAL_NOTIFY_NTFY_ENV_FILE": str(tmp_path / "nope.env")},
        ),
    )
    assert res.degraded is True
    assert "env file unreadable" in res.failures["ntfy"]


def test_cli_exits_nonzero_when_ntfy_fails(tmp_path, monkeypatch, capsys):
    """The acceptance criterion: a failed Tier-1 delivery is a non-zero result."""
    import urllib.error

    _capture_urlopen(monkeypatch, error=urllib.error.URLError("down"))
    monkeypatch.setenv("PRINCIPAL_NOTIFY_BACKENDS", "ntfy")
    monkeypatch.setenv("PRINCIPAL_NOTIFY_NTFY_URL", "http://x:1")
    monkeypatch.setenv("PRINCIPAL_NOTIFY_NTFY_TOPIC", "t")
    rc = principal_notify.main(
        ["--subject", "s", "--body", "b", "--workspace", str(tmp_path)]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["degraded"] is True
    assert out["operator_notified"] is False
    assert Path(out["local_alert_path"]).exists()
