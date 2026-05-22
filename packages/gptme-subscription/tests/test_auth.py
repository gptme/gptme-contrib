"""Tests for credential expiry detection."""

from __future__ import annotations

import json
import time
from pathlib import Path

from gptme_subscription.auth import (
    check_credential_file,
    format_reauth_instructions,
    probe_credential,
)


def _write_credential(path: Path, *, expires_at_ms: int, sub_type: str = "max") -> None:
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "fake-access",
                    "refreshToken": "fake-refresh",
                    "expiresAt": expires_at_ms,
                    "scopes": ["user:inference"],
                    "subscriptionType": sub_type,
                    "rateLimitTier": "max",
                }
            }
        )
    )


def test_check_credential_file_missing(tmp_path: Path) -> None:
    info = check_credential_file(tmp_path / "nope.json", "alice")
    assert info.status == "missing"
    assert info.needs_reauth_hint is True
    assert info.expires_at is None


def test_check_credential_file_valid(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    future = int((time.time() + 3600) * 1000)
    _write_credential(path, expires_at_ms=future)

    info = check_credential_file(path, "alice")
    assert info.status == "valid"
    assert info.expires_at == future
    assert info.expires_in_seconds is not None
    assert info.expires_in_seconds > 0
    assert info.subscription_type == "max"
    assert info.needs_reauth_hint is False


def test_check_credential_file_stale(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    past = int((time.time() - 3600) * 1000)
    _write_credential(path, expires_at_ms=past)

    info = check_credential_file(path, "alice")
    assert info.status == "stale"
    assert info.expires_in_seconds is not None
    assert info.expires_in_seconds < 0
    # Stale does NOT need re-auth — refresh will rotate the access token.
    assert info.needs_reauth_hint is False


def test_check_credential_file_malformed(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"some": "other shape"}))

    info = check_credential_file(path, "alice")
    assert info.status == "malformed"
    assert info.needs_reauth_hint is True


def test_check_credential_file_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text("{not json at all")
    info = check_credential_file(path, "alice")
    assert info.status == "malformed"


def test_check_credential_file_missing_expires_at(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    info = check_credential_file(path, "alice")
    assert info.status == "malformed"


def test_probe_credential_skips_without_usage_script(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    _write_credential(path, expires_at_ms=int((time.time() + 3600) * 1000))
    info, ok, msg = probe_credential(path, "alice", usage_script=None)
    assert ok is True
    assert "skipped" in msg


def test_probe_credential_fails_on_missing_file(tmp_path: Path) -> None:
    info, ok, msg = probe_credential(tmp_path / "nope.json", "alice")
    assert ok is False
    assert info.status == "missing"


def test_probe_credential_uses_target_slot_as_live_credential(tmp_path: Path) -> None:
    path = tmp_path / "alice.json"
    _write_credential(path, expires_at_ms=int((time.time() + 3600) * 1000))
    script = tmp_path / "probe.py"
    script.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys

live = Path.home() / ".claude" / ".credentials.json"
target = live.resolve()
text = target.read_text()
sys.exit(0 if target.name == "alice.json" and "fake-refresh" in text else 7)
"""
    )
    script.chmod(0o755)

    info, ok, msg = probe_credential(path, "alice", usage_script=script)

    assert info.status == "valid"
    assert ok is True
    assert msg == "probe ok"


def test_probe_credential_reports_probe_os_error(tmp_path: Path) -> None:
    path = tmp_path / "alice.json"
    _write_credential(path, expires_at_ms=int((time.time() + 3600) * 1000))
    script = tmp_path / "probe.py"
    script.write_text("#!/usr/bin/env python3\nprint('ok')\n")
    script.chmod(0o644)

    info, ok, msg = probe_credential(path, "alice", usage_script=script)

    assert info.status == "valid"
    assert ok is False
    assert msg.startswith("probe error:")


def test_format_reauth_instructions_includes_slot(tmp_path: Path) -> None:
    out = format_reauth_instructions("alice")
    assert ".credentials.json.alice" in out
    assert "claude" in out  # "claude" CLI mention
    assert "/login" in out


def test_format_reauth_instructions_relinks_after_login() -> None:
    """The ln -sf step must come AFTER /login.

    /login (and routine token refresh) replaces the live symlink with a regular
    file, so re-linking before /login is clobbered and leaves credential drift.
    The symlink restore must be the last filesystem step. Regression guard for
    the 2026-05-22 reauth-order fix.
    """
    out = format_reauth_instructions("bob")
    login_idx = out.index("/login")
    relink_idx = out.index("ln -sf .credentials.json.bob ~/.claude/.credentials.json")
    cp_idx = out.index("cp ~/.claude/.credentials.json ~/.claude/.credentials.json.bob")
    assert login_idx < cp_idx < relink_idx


def test_to_dict_omits_none_fields(tmp_path: Path) -> None:
    info = check_credential_file(tmp_path / "missing.json", "alice")
    d = info.to_dict()
    assert "expires_at" not in d
    assert d["status"] == "missing"
    assert d["sub"] == "alice"
