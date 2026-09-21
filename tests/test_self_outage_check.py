"""Tests for the harness-agnostic self-outage detector.

Covers the pure signal logic (assess) and config resolution. The GitHub
escalation path is not exercised here — it shells out to `gh` — but the alert
file lifecycle and the no-repo skip are covered via monkeypatching `_gh`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "self-outage-check.py"
SPEC = importlib.util.spec_from_file_location("self_outage_check", SCRIPT)
assert SPEC and SPEC.loader
soc = importlib.util.module_from_spec(SPEC)
sys.modules["self_outage_check"] = soc
SPEC.loader.exec_module(soc)

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def write_records(workspace: Path, records: list[dict]) -> None:
    rf = workspace / "state" / "sessions" / "session-records.jsonl"
    rf.parent.mkdir(parents=True, exist_ok=True)
    rf.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def rec(hours_ago: float, outcome: str, reason: str | None = None) -> dict:
    r = {
        "timestamp": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "outcome": outcome,
    }
    if reason is not None:
        r["failure_reason"] = reason
    return r


def cfg_for(workspace: Path, repo: str | None = "owner/repo") -> Any:
    return soc.Config(
        workspace=workspace,
        agent_name="testagent",
        repo=repo,
        reasons=soc.DEFAULT_AUTH_OUTAGE_REASONS,
        reauth_cmd="run the reauth script",
    )


def assess(workspace: Path) -> Any:
    return soc.assess(
        cfg_for(workspace).records_file,
        soc.DEFAULT_AUTH_OUTAGE_REASONS,
        window_hours=36.0,
        min_failures=3,
        min_outage_hours=18.0,
        now=NOW,
    )


def test_no_records_is_healthy(tmp_path: Path) -> None:
    status = assess(tmp_path)
    assert status["outage"] is False
    assert status["auth_failures_in_window"] == 0
    assert status["last_productive_at"] is None


def test_sustained_auth_outage_is_confirmed(tmp_path: Path) -> None:
    # 4 auth failures in window, last productive 40h ago (outside the 36h window).
    write_records(
        tmp_path,
        [rec(40, "productive")]
        + [rec(h, "failed", "api_error_401") for h in (1, 2, 3, 4)],
    )
    status = assess(tmp_path)
    assert status["auth_failures_in_window"] == 4
    assert status["productive_in_window"] == 0
    assert status["outage"] is True


def test_recent_productive_blocks_outage(tmp_path: Path) -> None:
    # Same failures, but a productive session 2h ago -> not an outage.
    write_records(
        tmp_path,
        [rec(2, "productive")]
        + [rec(h, "failed", "api_error_401") for h in (1, 3, 4, 5)],
    )
    status = assess(tmp_path)
    assert status["productive_in_window"] == 1
    assert status["outage"] is False


def test_transient_failures_are_not_outage(tmp_path: Path) -> None:
    # 429/5xx are not auth outages even with no productive work.
    write_records(
        tmp_path,
        [rec(h, "failed", "api_error_429") for h in (1, 2, 3, 4)],
    )
    status = assess(tmp_path)
    assert status["auth_failures_in_window"] == 0
    assert status["outage"] is False


def test_below_min_failures_is_not_outage(tmp_path: Path) -> None:
    write_records(tmp_path, [rec(h, "failed", "auth_failure") for h in (1, 2)])
    status = assess(tmp_path)
    assert status["auth_failures_in_window"] == 2
    assert status["outage"] is False


def test_escalate_writes_and_clears_alert_file(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_gh(args: list[str]):
        calls.append(args)

        class R:
            returncode = 0
            stdout = "[]" if args[0] == "issue" and args[1] == "list" else "created"
            stderr = ""

        return R()

    monkeypatch.setattr(soc, "_gh", fake_gh)
    cfg = cfg_for(tmp_path)

    # Outage -> alert file written, issue created.
    write_records(
        tmp_path,
        [rec(40, "productive")]
        + [rec(h, "failed", "auth_failure") for h in (1, 2, 3, 4)],
    )
    status = assess(tmp_path)
    assert status["outage"] is True
    soc.escalate(cfg, status, dry_run=False, now=NOW)
    assert cfg.alert_file.exists()
    assert any(a[:2] == ["issue", "create"] for a in calls)

    # Recovery -> alert file cleared.
    write_records(tmp_path, [rec(1, "productive")])
    healthy = assess(tmp_path)
    assert healthy["outage"] is False
    soc.escalate(cfg, healthy, dry_run=False, now=NOW)
    assert not cfg.alert_file.exists()


def test_no_repo_skips_github_but_writes_alert(tmp_path: Path, monkeypatch) -> None:
    def boom(args: list[str]):
        raise AssertionError("gh should not be called without a repo")

    monkeypatch.setattr(soc, "_gh", boom)
    cfg = cfg_for(tmp_path, repo=None)
    write_records(
        tmp_path,
        [rec(40, "productive")]
        + [rec(h, "failed", "auth_failure") for h in (1, 2, 3, 4)],
    )
    status = assess(tmp_path)
    soc.escalate(cfg, status, dry_run=False, now=NOW)
    assert cfg.alert_file.exists()


def test_auth_reason_matches_default(tmp_path: Path) -> None:
    # "auth" is what the session recorder emits for 401/403 — must match by default.
    write_records(
        tmp_path,
        [rec(40, "productive")] + [rec(h, "failed", "auth") for h in (1, 2, 3, 4)],
    )
    status = assess(tmp_path)
    assert status["auth_failures_in_window"] == 4
    assert status["outage"] is True


def test_no_history_recent_failures_not_outage(tmp_path: Path) -> None:
    # Fresh workspace: failures only 1-4h ago don't meet the 18h duration threshold.
    # The earliest failure age (4h) is used as the proxy; 4 < 18 → no outage.
    write_records(
        tmp_path,
        [rec(h, "failed", "api_error_401") for h in (1, 2, 3, 4)],
    )
    status = assess(tmp_path)
    assert status["auth_failures_in_window"] == 4
    assert (
        round(status["hours_since_last_productive"], 0) == 4.0
    )  # earliest failure proxy
    assert status["outage"] is False


def test_no_history_sustained_failures_is_outage(tmp_path: Path) -> None:
    # Fresh workspace: failures spanning 20h satisfy the 18h duration threshold.
    write_records(
        tmp_path,
        [rec(h, "failed", "api_error_401") for h in (1, 10, 15, 20)],
    )
    status = assess(tmp_path)
    assert status["auth_failures_in_window"] == 4
    assert (
        round(status["hours_since_last_productive"], 0) == 20.0
    )  # earliest failure proxy
    assert status["outage"] is True


def test_dry_run_does_not_write_alert_file(tmp_path: Path, monkeypatch) -> None:
    def fake_gh(args: list[str]):
        class R:
            returncode = 0
            stdout = "[]" if args[0] == "issue" and args[1] == "list" else "created"
            stderr = ""

        return R()

    monkeypatch.setattr(soc, "_gh", fake_gh)
    cfg = cfg_for(tmp_path)
    write_records(
        tmp_path,
        [rec(40, "productive")]
        + [rec(h, "failed", "auth_failure") for h in (1, 2, 3, 4)],
    )
    status = assess(tmp_path)
    assert status["outage"] is True
    soc.escalate(cfg, status, dry_run=True, now=NOW)
    assert not cfg.alert_file.exists()


def test_aged_out_failures_do_not_clear_alert(tmp_path: Path, monkeypatch) -> None:
    # auth failures aged out of window, productive still 0 — must NOT close issue.
    calls: list[list[str]] = []

    def fake_gh(args: list[str]):
        calls.append(args)

        class R:
            returncode = 0
            stdout = "[]"
            stderr = ""

        return R()

    monkeypatch.setattr(soc, "_gh", fake_gh)
    cfg = cfg_for(tmp_path)

    # First: trigger outage to create alert file.
    write_records(
        tmp_path,
        [rec(40, "productive")]
        + [rec(h, "failed", "auth_failure") for h in (1, 2, 3, 4)],
    )
    outage_status = assess(tmp_path)
    soc.escalate(cfg, outage_status, dry_run=False, now=NOW)
    assert cfg.alert_file.exists()

    # Now: failures aged out, but still no productive sessions in window.
    aged_status = {
        **outage_status,
        "outage": False,
        "auth_failures_in_window": 0,
        "productive_in_window": 0,
    }
    soc.escalate(cfg, aged_status, dry_run=False, now=NOW)
    # Alert file must survive — outage is still active.
    assert cfg.alert_file.exists()
    # gh must NOT have been called for a close.
    assert not any(a[:2] == ["issue", "close"] for a in calls)


def test_custom_reasons_override(tmp_path: Path) -> None:
    write_records(
        tmp_path,
        [rec(40, "productive")]
        + [rec(h, "failed", "my_custom_reason") for h in (1, 2, 3, 4)],
    )
    status = soc.assess(
        cfg_for(tmp_path).records_file,
        frozenset({"my_custom_reason"}),
        window_hours=36.0,
        min_failures=3,
        min_outage_hours=18.0,
        now=NOW,
    )
    assert status["auth_failures_in_window"] == 4
    assert status["outage"] is True
