from __future__ import annotations

import fcntl
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kuma_push.py"
SPEC = importlib.util.spec_from_file_location("kuma_push", SCRIPT)
assert SPEC and SPEC.loader
kuma_push = importlib.util.module_from_spec(SPEC)
sys.modules["kuma_push"] = kuma_push
SPEC.loader.exec_module(kuma_push)


class _Completed:
    def __init__(self, returncode=0, stdout='{"ok": true, "msg": "UP"}', stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_build_push_url_encodes_spaces():
    """The regression this helper exists for: an unencoded space makes curl exit 3."""
    url = kuma_push.build_push_url(
        "http://kuma:3001/api/push/abc", "up", "off-hours probe ok"
    )
    assert " " not in url
    assert "off-hours%20probe%20ok" in url
    assert url.endswith("?status=up&msg=off-hours%20probe%20ok")


def test_build_push_url_replaces_kuma_generated_parameters():
    url = kuma_push.build_push_url(
        "http://kuma/api/push/abc?status=up&msg=OK&ping=&status=down#fragment",
        "up",
        "healthy & ready",
    )
    assert url == (
        "http://kuma/api/push/abc?ping=&status=up&msg=healthy%20%26%20ready#fragment"
    )


def test_build_push_url_respects_existing_query():
    url = kuma_push.build_push_url("http://kuma/api/push/abc?foo=1", "down", "x")
    assert url == "http://kuma/api/push/abc?foo=1&status=down&msg=x"


@pytest.mark.parametrize(
    "body,expected",
    [
        ('{"ok": true}', True),
        ('{"ok":true,"msg":"UP"}', True),
        ('{"ok": false}', False),
        ('{"ok": "true"}', False),
        ("not json", False),
        ("", False),
        ('["ok"]', False),
    ],
)
def test_response_ok(body, expected):
    assert kuma_push.response_ok(body) is expected


def test_push_returns_true_and_records_ack(tmp_path, monkeypatch):
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        return _Completed()

    monkeypatch.setattr(kuma_push.subprocess, "run", fake_run)
    ack = tmp_path / "ack.json"
    assert kuma_push.push_url(
        "http://kuma/push/x", "up", "healthy", key="K", ack_path=ack
    )
    assert any("/push/x?status=up&msg=healthy" in str(a) for a in calls["args"])
    recorded = json.loads(ack.read_text())
    assert "K" in recorded
    assert "_since" in recorded


def test_push_nonzero_rc_is_logged_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        kuma_push.subprocess,
        "run",
        lambda *a, **k: _Completed(returncode=3, stderr="malformed"),
    )
    assert not kuma_push.push_url(
        "http://kuma/push/x", "up", "x", key="K", ack_path=tmp_path / "ack.json"
    )
    err = capsys.readouterr().err
    assert "curl rc=3" in err
    assert "K" not in kuma_push.load_acks(tmp_path / "ack.json")


def test_push_unacked_body_is_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        kuma_push.subprocess, "run", lambda *a, **k: _Completed(stdout='{"ok": false}')
    )
    assert not kuma_push.push_url("http://kuma/push/x", "up", "x")
    assert "not acknowledged" in capsys.readouterr().err


def test_push_curl_missing_is_failure(monkeypatch, capsys):
    def boom(*a, **k):
        raise FileNotFoundError("curl")

    monkeypatch.setattr(kuma_push.subprocess, "run", boom)
    assert not kuma_push.push_url("http://kuma/push/x", "up", "x")
    assert "failed" in capsys.readouterr().err


@pytest.mark.parametrize("returncode", [0, 3])
def test_push_diagnostics_never_echo_capability_url(monkeypatch, capsys, returncode):
    secret_url = "https://kuma.example/api/push/private-capability["
    monkeypatch.setattr(
        kuma_push.subprocess,
        "run",
        lambda *a, **k: _Completed(
            returncode=returncode, stdout=secret_url, stderr=f"bad range: {secret_url}"
        ),
    )
    assert not kuma_push.push_url(secret_url)
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "private-capability" not in err
    assert "kuma.example" not in err


def test_push_timeout_does_not_leak_capability_url(monkeypatch, capsys):
    secret_url = "https://kuma.example/api/push/private-capability"

    def timed_out(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(kuma_push.subprocess, "run", timed_out)
    assert not kuma_push.push_url(secret_url)
    err = capsys.readouterr().err
    assert "TimeoutExpired" in err
    assert "private-capability" not in err
    assert "kuma.example" not in err


def test_invalid_push_url_is_a_sanitized_failure(capsys):
    assert not kuma_push.push_url("http://[private-capability")
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "private-capability" not in err


def test_load_push_url_env_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / "push.env"
    env_file.write_text('KUMA_PUSH_URL_X="http://from-file"\nOTHER=1\n')
    monkeypatch.setenv("KUMA_PUSH_URL_X", "http://from-env")
    assert kuma_push.load_push_url("KUMA_PUSH_URL_X", env_file) == "http://from-env"
    monkeypatch.delenv("KUMA_PUSH_URL_X")
    assert kuma_push.load_push_url("KUMA_PUSH_URL_X", env_file) == "http://from-file"
    assert kuma_push.load_push_url("MISSING", env_file) is None


def test_unconfigured_key_is_silent(tmp_path, monkeypatch, capsys):
    """An unset key is a deploy-time state, not a repeated warning on every tick."""
    monkeypatch.delenv("KUMA_PUSH_URL_UNSET", raising=False)
    assert not kuma_push.push("KUMA_PUSH_URL_UNSET", env_file=tmp_path / "missing.env")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("operation", ["push", "configured_keys"])
def test_invalid_utf8_config_is_a_logged_failure(
    tmp_path, monkeypatch, capsys, operation
):
    env_file = tmp_path / "push.env"
    env_file.write_bytes(b"KUMA_PUSH_URL_X=http://kuma/private-capability\xff\n")
    monkeypatch.delenv("KUMA_PUSH_URL_X", raising=False)

    def no_network(*args, **kwargs):
        pytest.fail("invalid configuration must not attempt a heartbeat")

    monkeypatch.setattr(kuma_push.subprocess, "run", no_network)
    if operation == "push":
        assert not kuma_push.push("KUMA_PUSH_URL_X", env_file=env_file)
    else:
        assert kuma_push.configured_keys(env_file) == []
    err = capsys.readouterr().err
    assert "UnicodeDecodeError" in err
    assert "private-capability" not in err
    assert str(env_file) not in err


def test_configured_keys(tmp_path):
    env_file = tmp_path / "push.env"
    env_file.write_text("# comment\nKUMA_PUSH_URL_A=1\n\nKUMA_PUSH_URL_B=2\n")
    assert kuma_push.configured_keys(env_file) == ["KUMA_PUSH_URL_A", "KUMA_PUSH_URL_B"]
    assert kuma_push.configured_keys(tmp_path / "nope.env") == []


def test_record_ack_is_atomic_and_never_raises_on_bad_path(tmp_path, capsys):
    target = tmp_path / "nested" / "ack.json"
    assert kuma_push.record_ack("K", path=target)
    assert kuma_push.load_acks(target)["K"]
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".kuma-ack-")]

    # A directory where the file should be cannot be written: report, do not raise.
    bad = tmp_path / "dir-instead"
    bad.mkdir()
    assert kuma_push.record_ack("K", path=bad) is False
    assert "could not record Kuma ack" in capsys.readouterr().err


def test_record_ack_serializes_read_merge_replace(tmp_path):
    target = tmp_path / "ack.json"
    # Hold the same stable sidecar used by independent timer processes. A writer
    # must wait before reading, then merge the update made by the lock holder.
    with target.with_suffix(".json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        writer = subprocess.Popen(
            [sys.executable, str(SCRIPT), "record-ack", "B", "--ack-file", str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                writer.communicate(timeout=0.5)
            target.write_text(json.dumps({"A": "2026-11-13T00:00:00+00:00"}))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            writer.communicate(timeout=10)
    assert writer.returncode == 0
    assert set(kuma_push.load_acks(target)) == {"A", "B", "_since"}


def _ts(**delta):
    return datetime.now(timezone.utc) - timedelta(**delta)


def test_failed_first_push_starts_bootstrap_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kuma_push.subprocess, "run", lambda *a, **k: _Completed(returncode=7)
    )
    target = tmp_path / "ack.json"
    assert not kuma_push.push_url("http://kuma/push/x", key="K", ack_path=target)
    first = kuma_push.load_acks(target)
    assert "_since" in first
    assert "K" not in first
    started = datetime.fromisoformat(first["_since"])
    assert kuma_push.stale_acks(first, ["K"], started, {"K": 1}) == []
    assert not kuma_push.push_url("http://kuma/push/x", key="K", ack_path=target)
    later = kuma_push.load_acks(target)
    assert later["_since"] == first["_since"]
    problems = kuma_push.stale_acks(
        later, ["K"], started + timedelta(hours=4), {"K": 1}
    )
    assert len(problems) == 1 and "no acknowledged push" in problems[0]


def test_missing_bootstrap_is_not_reported_as_healthy():
    problems = kuma_push.stale_acks({}, ["K"], datetime.now(timezone.utc), {"K": 1})
    assert len(problems) == 1 and "tracking not initialized" in problems[0]


def test_stale_acks_reports_missing_and_stale_only_after_bootstrap():
    now = datetime.now(timezone.utc)
    intervals = {"KUMA_PUSH_URL_A": 2.0}
    # Tracking just started: a never-acked key is not yet dark.
    fresh = {"_since": now.isoformat()}
    assert kuma_push.stale_acks(fresh, ["KUMA_PUSH_URL_A"], now, intervals) == []
    # Tracking has been live long past the tolerance window.
    old = {"_since": _ts(hours=50).isoformat()}
    problems = kuma_push.stale_acks(old, ["KUMA_PUSH_URL_A"], now, intervals)
    assert len(problems) == 1 and "no acknowledged push" in problems[0]


def test_stale_acks_flags_overdue_ack():
    now = datetime.now(timezone.utc)
    acks = {
        "_since": _ts(days=3).isoformat(),
        "KUMA_PUSH_URL_A": _ts(hours=7).isoformat(),
        "KUMA_PUSH_URL_B": _ts(hours=1).isoformat(),
    }
    problems = kuma_push.stale_acks(
        acks,
        ["KUMA_PUSH_URL_A", "KUMA_PUSH_URL_B", "KUMA_PUSH_URL_UNKNOWN"],
        now,
        {"KUMA_PUSH_URL_A": 2.0, "KUMA_PUSH_URL_B": 2.0},
    )
    assert len(problems) == 1
    assert "KUMA_PUSH_URL_A" in problems[0]
    assert "last acknowledged" in problems[0]


def test_stale_acks_flags_unparseable_ack():
    now = datetime.now(timezone.utc)
    problems = kuma_push.stale_acks(
        {"KUMA_PUSH_URL_A": "yesterday"},
        ["KUMA_PUSH_URL_A"],
        now,
        {"KUMA_PUSH_URL_A": 1.0},
    )
    assert problems and "unparseable" in problems[0]


def test_cli_record_ack(tmp_path):
    assert (
        kuma_push.main(["record-ack", "K", "--ack-file", str(tmp_path / "a.json")]) == 0
    )
    assert kuma_push.load_acks(tmp_path / "a.json")["K"]


def test_cli_push_unconfigured_exits_nonzero(tmp_path):
    rc = kuma_push.main(
        [
            "push",
            "KUMA_PUSH_URL_NOPE",
            "--env-file",
            str(tmp_path / "none.env"),
            "--ack-file",
            str(tmp_path / "a.json"),
        ]
    )
    assert rc == 1
