"""check_credential_file promises a status ('malformed'/'unreadable') for bad
files, but some bad inputs raise instead, crashing --check-auth for all slots."""

from __future__ import annotations

import json
from pathlib import Path

from gptme_subscription.auth import check_credential_file


def test_non_utf8_file_is_classified_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_bytes(b'{"claudeAiOauth": {"expiresAt": 1}}\xff\xfe')
    info = check_credential_file(path, "alice")
    assert info.status in ("malformed", "unreadable")


def test_overflowing_expires_at_is_malformed_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    # 1e400 is valid JSON; Python parses it to float('inf').
    path.write_text('{"claudeAiOauth": {"expiresAt": 1e400}}')
    info = check_credential_file(path, "alice")
    assert info.status == "malformed"


def test_expires_at_too_large_for_float_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json.alice"
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 10**309}}))
    assert check_credential_file(path, "alice").status == "malformed"
