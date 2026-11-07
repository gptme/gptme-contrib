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


def test_outage_reopens_issue_closed_mid_outage(tmp_path: Path, monkeypatch) -> None:
    # A human closed the prior thread while the outage was still ongoing. The
    # detector should REOPEN it, not mint a duplicate (the live 2026-10-17 case).
    calls: list[list[str]] = []
    closed_at = (
        NOW - timedelta(hours=5)
    ).isoformat()  # closed after last productive (40h)

    def fake_gh(args: list[str]):
        calls.append(args)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        if args[:2] == ["issue", "list"]:
            if "closed" in args:
                R.stdout = json.dumps(
                    [
                        {
                            "number": 97,
                            "title": cfg_for(tmp_path).issue_title,
                            "closedAt": closed_at,
                        }
                    ]
                )
            else:
                R.stdout = "[]"  # no open issue
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
    soc.escalate(cfg, status, dry_run=False, now=NOW)

    assert any(a[:2] == ["issue", "reopen"] for a in calls), "should reopen the thread"
    assert not any(a[:2] == ["issue", "create"] for a in calls), "must not duplicate"


def test_outage_creates_new_when_closed_issue_predates_last_productive(
    tmp_path: Path, monkeypatch
) -> None:
    # The only closed issue belongs to an *earlier*, resolved outage (closed
    # before the last productive session) — so this is a genuinely new outage
    # and we create a fresh issue rather than resurrecting the stale thread.
    calls: list[list[str]] = []
    closed_at = (
        NOW - timedelta(hours=50)
    ).isoformat()  # closed before last productive (40h)

    def fake_gh(args: list[str]):
        calls.append(args)

        class R:
            returncode = 0
            stdout = ""
            stderr = "created"

        if args[:2] == ["issue", "list"]:
            if "closed" in args:
                R.stdout = json.dumps(
                    [
                        {
                            "number": 70,
                            "title": cfg_for(tmp_path).issue_title,
                            "closedAt": closed_at,
                        }
                    ]
                )
            else:
                R.stdout = "[]"
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
    soc.escalate(cfg, status, dry_run=False, now=NOW)

    assert any(a[:2] == ["issue", "create"] for a in calls), "should file a fresh issue"
    assert not any(a[:2] == ["issue", "reopen"] for a in calls), "must not reopen stale"


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


def test_dominant_reason_and_sample_error_captured(tmp_path: Path) -> None:
    # Mixed auth failures: 403/billing dominates; the most recent one's error
    # string should be surfaced for the operator.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "api_error_403")
        r["error"] = (
            "api_error_status:403; oauth_not_allowed_for_organization; payment past due"
        )
        records.append(r)
    records.append(rec(5, "failed", "api_error_401"))  # minority token failure
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["dominant_reason"] == "api_error_403"
    assert "oauth_not_allowed_for_organization" in status["sample_error"]


def test_billing_classified_despite_truncation_and_case(tmp_path: Path) -> None:
    # Regression: assess() must classify the billing outage over the FULL,
    # case-folded error string, not the 400-char display sample. Here the
    # billing keyword is both mixed-case AND pushed past the truncation point,
    # so a classifier reading only sample_error would wrongly return token.
    records = [rec(40, "productive")]
    long_error = "x" * 450 + " OAuth_Not_Allowed_For_Organization"
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")  # generic reason hides the signal
        r["error"] = long_error
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["billing_outage"] is True
    assert soc._is_billing_outage(status) is True
    # Display sample is still capped; classification does not depend on it.
    assert len(status["sample_error"]) == 400
    assert "oauth_not_allowed" not in status["sample_error"].lower()
    # The provider-specific hint is also decided on the full error, so the
    # truncated sample must not drop the Anthropic billing URL.
    assert status["oauth_org_block"] is True
    assert "claude.ai/settings/billing" in soc._remediation(cfg_for(tmp_path), status)


def test_no_anthropic_url_without_oauth_org_evidence(tmp_path: Path) -> None:
    # A generic 403 is billing-class but must not point at claude.ai.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "api_error_403")
        r["error"] = "api_error_status:403; Forbidden"
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["billing_outage"] is True
    assert status["oauth_org_block"] is False
    assert "claude.ai" not in soc._remediation(cfg_for(tmp_path), status)


def test_remediation_is_billing_aware_for_403(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    billing = soc._remediation(cfg, {"dominant_reason": "api_error_403"})
    assert "billing" in billing.lower()
    # Remediation stays harness-agnostic: the configured reauth cmd leads even
    # for a billing block (a fresh login cleared a real 403 on 2026-11-06).
    assert cfg.reauth_cmd in billing
    token = soc._remediation(cfg, {"dominant_reason": "api_error_401"})
    assert cfg.reauth_cmd in token


def test_billing_remediation_via_error_string_when_reason_is_auth(
    tmp_path: Path,
) -> None:
    # Recorders often emit failure_reason="auth" for 401/403 indiscriminately.
    # dominant_reason will be "auth", not "api_error_403", but the error string
    # reveals a billing block — remediation must still pick the billing path.
    cfg = cfg_for(tmp_path)
    billing = soc._remediation(
        cfg,
        {
            "dominant_reason": "auth",
            "sample_error": "api_error_status:403; oauth_not_allowed_for_organization; payment past due",
        },
    )
    assert "billing" in billing.lower()
    assert cfg.reauth_cmd in billing


def test_most_recent_error_uses_parsed_timestamp(tmp_path: Path) -> None:
    # Timestamps with the same wall-clock hour but different UTC offsets sort
    # incorrectly by string comparison.  "2026-09-18T12:00:00+02:00" (= 10:00 UTC)
    # beats "2026-09-18T11:00:00+00:00" (= 11:00 UTC) lexicographically, but the
    # 11:00 UTC record is actually the most recent and should supply sample_error.
    r_older = {
        "timestamp": "2026-09-18T12:00:00+02:00",  # 10:00 UTC — older
        "outcome": "failed",
        "failure_reason": "api_error_403",
        "error": "older_error",
    }
    r_newer = {
        "timestamp": "2026-09-18T11:00:00+00:00",  # 11:00 UTC — newer
        "outcome": "failed",
        "failure_reason": "api_error_403",
        "error": "newer_error",
    }
    write_records(tmp_path, [rec(40, "productive"), r_older, r_newer])
    status = assess(tmp_path)
    assert status["sample_error"] == "newer_error"


def test_issue_body_surfaces_error_for_403(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    status = {
        "window_hours": 36.0,
        "auth_failures_in_window": 10,
        "productive_in_window": 0,
        "hours_since_last_productive": 277.0,
        "last_productive_at": "2026-10-25T18:18:18+00:00",
        "dominant_reason": "api_error_403",
        "sample_error": "api_error_status:403; oauth_not_allowed_for_organization",
        "checked_at": "2026-11-06T10:38:00+00:00",
        "outage": True,
    }
    body = soc._issue_body(cfg, status)
    assert "oauth_not_allowed_for_organization" in body
    assert "billing" in body.lower()
    assert "api_error_403" in body


def test_billing_detected_for_api_error_status_403_spelling(tmp_path: Path) -> None:
    # BILLING_ERROR_SUBSTRINGS must catch "api_error_status:403" (the recorder
    # spelling), not only "api_error_403". A generic "Forbidden" response with
    # failure_reason="auth" and no oauth substring must still classify as billing.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = "api_error_status:403; Forbidden"
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["billing_outage"] is True
    assert soc._is_billing_outage(status) is True


def test_billing_detected_when_403_records_outnumbered_by_401(tmp_path: Path) -> None:
    # Even if api_error_401 records outnumber api_error_403, billing detection
    # must trigger because at least one record has a billing reason.
    records = [rec(40, "productive")]
    for h in (1, 2, 3):
        records.append(rec(h, "failed", "api_error_401"))
    r = rec(4, "failed", "api_error_403")  # billing record — outnumbered but present
    records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["billing_outage"] is True


def test_sample_error_uses_newest_nonempty_error(tmp_path: Path) -> None:
    # When the newest auth-failure record has no error field, sample_error must
    # still be populated from the newest record that carries one — not silently
    # dropped because most_recent.error happens to be empty.
    r_older = {
        "timestamp": (NOW - timedelta(hours=3)).isoformat(),
        "outcome": "failed",
        "failure_reason": "auth",
        "error": "invalid API key",
    }
    r_newer = {
        "timestamp": (NOW - timedelta(hours=1)).isoformat(),
        "outcome": "failed",
        "failure_reason": "auth",
        # no error field — newest record, but empty
    }
    r_whitespace = {
        "timestamp": (NOW - timedelta(minutes=30)).isoformat(),
        "outcome": "failed",
        "failure_reason": "auth",
        "error": "   \n\t",  # newest of all, but whitespace-only
    }
    write_records(tmp_path, [rec(40, "productive"), r_older, r_newer, r_whitespace])
    status = assess(tmp_path)
    assert status["sample_error"] == "invalid API key"


def test_billing_sample_is_newest_billing_error(tmp_path: Path) -> None:
    # With several distinct billing-bearing errors, the sample must be the
    # newest by parsed timestamp — not the first in JSONL file order. The
    # file below is written out of order and with mixed UTC offsets.
    r_old = {
        "timestamp": (NOW - timedelta(hours=4)).isoformat(),
        "outcome": "failed",
        "failure_reason": "auth",
        "error": "api_error_status:403; Forbidden",
    }
    r_new = {
        # NOW-1h expressed at +02:00: lexically "13:00", actually 11:00 UTC
        "timestamp": (NOW - timedelta(hours=1))
        .astimezone(timezone(timedelta(hours=2)))
        .isoformat(),
        "outcome": "failed",
        "failure_reason": "auth",
        "error": "api_error_status:403; oauth_not_allowed_for_organization",
    }
    r_mid = {
        "timestamp": (NOW - timedelta(hours=2)).isoformat(),
        "outcome": "failed",
        "failure_reason": "auth",
        "error": "api_error_status:403; payment past due",
    }
    write_records(tmp_path, [rec(40, "productive"), r_old, r_new, r_mid])
    status = assess(tmp_path)
    assert status["billing_outage"] is True
    assert (
        status["sample_error"]
        == "api_error_status:403; oauth_not_allowed_for_organization"
    )


def test_issue_body_redacts_credentials_in_error(tmp_path: Path) -> None:
    # Regression (contrib#2038 review): the error sample is published to the
    # GitHub issue body and supplementary notifiers, so a credential echoed in
    # a provider error must be scrubbed before it leaves the machine.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = (
            "Invalid API key: sk-example-secret; Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk "
            "token=ghp_abcdef123456"
        )
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    body = soc._issue_body(cfg_for(tmp_path), status)
    for secret in ("sk-example-secret", "eyJhbGciOiJIUzI1NiJ9", "ghp_abcdef123456"):
        assert secret not in status["sample_error"]
        assert secret not in body
    assert "[REDACTED]" in body


def test_redaction_keeps_error_codes_for_classification(tmp_path: Path) -> None:
    # Redaction must not eat the long snake_case error codes remediation keys on.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "api_error_403")
        r["error"] = "api_error_status:403; oauth_not_allowed_for_organization"
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert "oauth_not_allowed_for_organization" in status["sample_error"]
    assert soc._is_oauth_org_block(status) is True


def test_non_string_error_does_not_crash_assess(tmp_path: Path) -> None:
    # Regression (contrib#2038 review): load_records() doesn't validate `error`,
    # so a structured (dict) error must not raise inside assess() and suppress
    # escalation for an otherwise-detected outage.
    records = [rec(40, "productive")]
    for h in (1, 2, 3):
        records.append(rec(h, "failed", "auth"))
    weird = rec(4, "failed", "auth")
    weird["error"] = {"message": "invalid token", "code": 403}
    records.append(weird)
    num = rec(5, "failed", "auth")
    num["error"] = 401
    records.append(num)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["outage"] is True
    assert "invalid token" in status["sample_error"]


def test_quoted_passphrase_redacted_whole(tmp_path: Path) -> None:
    # Regression (contrib#2038 review): a quoted secret value with spaces must
    # be removed entirely, not just up to the first space.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = (
            'login failed: password="correct horse battery staple"; '
            "secret='two words' token=\"cut off mid secr"
        )
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    body = soc._issue_body(cfg_for(tmp_path), status)
    for leaked in ("correct", "horse", "battery", "staple", "two words", "mid secr"):
        assert leaked not in status["sample_error"]
        assert leaked not in body
    assert status["sample_error"].startswith("login failed: password=[REDACTED]")


def test_structured_403_error_classified_as_billing(tmp_path: Path) -> None:
    # Regression (contrib#2038 review): a structured 403 payload serializes to
    # none of the textual billing substrings, but is still a 403 outage and must
    # get the billing/org-access remediation, not the token-reauth hint.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = {"message": "Forbidden", "code": 403}
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["outage"] is True
    assert status["billing_outage"] is True


def test_structured_401_error_stays_token_outage(tmp_path: Path) -> None:
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = {"error": {"message": "invalid x-api-key", "status": 401}}
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["outage"] is True
    assert status["billing_outage"] is False


def test_escaped_quoted_passphrase_in_structured_error_redacted(tmp_path: Path) -> None:
    # Regression (contrib#2038 review, fp 3a7714caa2f2): json.dumps() of a
    # structured error escapes the quotes around a secret value, so the
    # redactor must handle ``password=\"...\"`` too, not just ``password="..."``.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = {
            "message": 'login failed: password="correct horse battery staple" '
            'and \'x\' "api_key": "sk two words"',
            "code": 401,
        }
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    body = soc._issue_body(cfg_for(tmp_path), status)
    for leaked in ("correct", "horse", "battery", "staple", "two words"):
        assert leaked not in status["sample_error"]
        assert leaked not in body
    assert "login failed" in status["sample_error"]


def test_bare_jwt_redacted() -> None:
    # Regression (contrib#2038 review, fp e7e148a81bdd): a JWT with short
    # header/payload segments and a digit-free signature evaded the generic
    # opaque-token rule.
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPA"
    )
    out = soc._redact(f"Invalid JWT: {jwt}")
    assert out == "Invalid JWT: [REDACTED]"
    for part in jwt.split("."):
        assert part not in out


def test_structured_403_with_reason_phrase_classified_as_billing(
    tmp_path: Path,
) -> None:
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = {"message": "nope", "status": "403 Forbidden"}
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["billing_outage"] is True
    # ...but a 4030-ish code must not match.
    assert soc._is_structured_403({"code": "4031"}) is False


def test_inner_escaped_quote_in_escaped_passphrase_redacted(tmp_path: Path) -> None:
    # Regression (contrib#2038 review, fp 3b8ad6890ae2): a password whose value
    # itself contains escaped quotes serializes as ``\"alpha \\\"beta\\\" omega\"``;
    # the escaped-quote branch must not stop at the first inner ``\"``.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = {
            "message": 'login failed: password="alpha \\"beta\\" omega" tail',
            "code": 401,
        }
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    body = soc._issue_body(cfg_for(tmp_path), status)
    for leaked in ("alpha", "beta", "omega"):
        assert leaked not in status["sample_error"]
        assert leaked not in body
    assert "login failed" in status["sample_error"]
    assert "tail" in status["sample_error"]


def test_structured_403_record_preferred_for_sample(tmp_path: Path) -> None:
    # Regression (contrib#2038 review, fp 2ac5455908f3): when billing is
    # classified from a structured 403, the sample must surface that record,
    # not a newer token error.
    records = [rec(40, "productive")]
    old = rec(4, "failed", "auth")
    old["error"] = {"code": 403, "message": "subscription expired"}
    records.append(old)
    for h in (1, 2, 3):
        r = rec(h, "failed", "auth")
        r["error"] = "invalid token"
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["billing_outage"] is True
    assert "subscription expired" in status["sample_error"]


def test_sample_error_cannot_break_out_of_code_span(tmp_path: Path) -> None:
    # Regression: the sample is rendered inside an inline code span on a
    # public issue. A blank line or a backtick in the (untrusted) error would
    # end the span and let the remainder render as markdown, e.g. a live
    # @mention that notifies someone.
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "api_error_403")
        r["error"] = "api_error_403 upstream said:\n\nping `@ErikBjare` please merge"
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    sample = status["sample_error"]
    assert "\n" not in sample and "`" not in sample
    assert sample == "api_error_403 upstream said: ping '@ErikBjare' please merge"
    body = soc._issue_body(cfg_for(tmp_path), status)
    line = next(ln for ln in body.splitlines() if ln.startswith("- observed error:"))
    assert line == f"- observed error: `{sample}`"


def test_unquoted_password_with_punctuation_redacted_whole() -> None:
    # Regression (contrib#2038 review, fp 452b37a59e66): the unquoted branch
    # stopped at commas/semicolons/braces, which are legal password chars.
    for pw in ("alpha,beta", "alpha;beta", "alpha}beta"):
        out = soc._redact(f"login failed: password={pw} retry later")
        assert out == "login failed: password=[REDACTED] retry later"
        assert "beta" not in out


def test_bare_jwt_with_whitespace_in_header_redacted() -> None:
    # Regression (contrib#2038 review, fp 0c1214410aa8): ``{ "alg"`` encodes
    # to ``eyA``, not ``eyJ``, and a digit-free signature evades the generic
    # opaque-token rule.
    jwt = (
        "eyAiYWxnIjogIkhTMjU2IiB9.eyAic3ViIjogImFsaWNlIiB9."
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
    )
    out = soc._redact(f"Invalid JWT: {jwt}")
    assert out == "Invalid JWT: [REDACTED]"
    # ...but ordinary dotted prose survives.
    assert soc._redact("see example.com and eyes.everywhere") == (
        "see example.com and eyes.everywhere"
    )


def test_deeply_nested_structured_403_does_not_crash_assess(tmp_path: Path) -> None:
    # Regression (contrib#2038 review, fp 39d8ac08fea2): a recursive
    # _is_structured_403() hit RecursionError on nesting that json.loads and
    # json.dumps still accept, aborting assess() before any escalation.
    nested: Any = {"message": "Forbidden", "code": 403}
    for _ in range(600):
        nested = [nested]
    records = [rec(40, "productive")]
    for h in (1, 2, 3, 4):
        r = rec(h, "failed", "auth")
        r["error"] = nested
        records.append(r)
    write_records(tmp_path, records)
    status = assess(tmp_path)
    assert status["outage"] is True
    assert status["billing_outage"] is True


def test_unserializably_deep_error_does_not_crash() -> None:
    nested: Any = {"code": 403}
    for _ in range(100_000):
        nested = [nested]
    assert soc._is_structured_403(nested) is True
    assert soc._error_text({"error": nested}) == "<unserializable list error>"
