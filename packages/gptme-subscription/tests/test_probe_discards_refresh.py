"""probe_credential runs the usage script against a throwaway HOME whose
.credentials.json is a symlink to the slot. Claude Code's token refresh
REPLACES that symlink with a regular file (documented in auth.REAUTH_INSTRUCTIONS),
so the rotated refresh token lands in the temp dir and is deleted with it.
The slot file keeps the already-consumed refresh token."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from gptme_subscription.auth import probe_credential


def _write_slot(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": int((time.time() - 3600) * 1000),  # stale
                }
            }
        )
    )


def test_probe_refresh_is_persisted_to_slot(tmp_path: Path) -> None:
    slot = tmp_path / ".credentials.json.alice"
    _write_slot(slot)
    script = tmp_path / "usage.py"
    # Mimic Claude Code's refresh: write new tokens via temp file + rename onto
    # $HOME/.claude/.credentials.json (which replaces the symlink).
    script.write_text(
        """#!/usr/bin/env python3
import json, os
from pathlib import Path
live = Path.home() / ".claude" / ".credentials.json"
data = json.loads(live.read_text())
data["claudeAiOauth"].update(accessToken="new-access", refreshToken="new-refresh",
                             expiresAt=99999999999999)
tmp = live.with_name(".credentials.json.tmp")
tmp.write_text(json.dumps(data))
os.replace(tmp, live)
print(json.dumps({"seven_day": {"utilization": 0.1}}))
"""
    )
    script.chmod(0o755)

    _, ok, _ = probe_credential(slot, "alice", usage_script=script)
    assert ok is True

    oauth = json.loads(slot.read_text())["claudeAiOauth"]
    assert oauth["refreshToken"] == "new-refresh", (
        "rotated refresh token from the probe was discarded; slot still holds "
        f"{oauth['refreshToken']!r}"
    )


def _write_account_slot(path: Path, account: str) -> None:
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": int((time.time() - 3600) * 1000),
                    "accountUuid": account,
                }
            }
        )
    )


def _refresh_script(tmp_path: Path, account_update: str) -> Path:
    script = tmp_path / "usage.py"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, os
from pathlib import Path
live = Path.home() / ".claude" / ".credentials.json"
data = json.loads(live.read_text())
oauth = data["claudeAiOauth"]
oauth.update(accessToken="new-access", refreshToken="new-refresh",
             expiresAt=99999999999999)
{account_update}
tmp = live.with_name(".credentials.json.tmp")
tmp.write_text(json.dumps(data))
os.replace(tmp, live)
print(json.dumps({{"seven_day": {{"utilization": 0.1}}}}))
"""
    )
    script.chmod(0o755)
    return script


@pytest.mark.parametrize(
    "account_update",
    [
        'oauth["accountUuid"] = "account-b"',
        'oauth.pop("accountUuid")',
    ],
    ids=["other-account", "no-account"],
)
def test_probe_refresh_never_overwrites_slot_of_another_account(
    tmp_path: Path, account_update: str
) -> None:
    slot = tmp_path / ".credentials.json.alice"
    _write_account_slot(slot, "account-a")
    script = _refresh_script(tmp_path, account_update)

    _, ok, message = probe_credential(slot, "alice", usage_script=script)
    assert ok, message  # the script ran and refreshed

    oauth = json.loads(slot.read_text())["claudeAiOauth"]
    assert oauth["accountUuid"] == "account-a"
    assert oauth["refreshToken"] == "old-refresh"


def test_probe_refresh_for_same_account_is_persisted(tmp_path: Path) -> None:
    slot = tmp_path / ".credentials.json.alice"
    _write_account_slot(slot, "account-a")
    script = _refresh_script(tmp_path, "")

    _, ok, message = probe_credential(slot, "alice", usage_script=script)
    assert ok, message  # the script ran and refreshed

    oauth = json.loads(slot.read_text())["claudeAiOauth"]
    assert oauth["accountUuid"] == "account-a"
    assert oauth["refreshToken"] == "new-refresh"


def test_probe_refresh_is_persisted_when_probe_times_out(tmp_path: Path) -> None:
    # The refresh lands before the usage fetch; a hang after it must not lose
    # the rotated token with the temporary directory.
    slot = tmp_path / ".credentials.json.alice"
    _write_account_slot(slot, "account-a")
    script = _refresh_script(tmp_path, "")
    text = script.read_text().replace(
        "print(json.dumps(", "import time; time.sleep(30)\nprint(json.dumps("
    )
    script.write_text(text)

    _, ok, message = probe_credential(slot, "alice", usage_script=script, timeout=2)

    assert ok is False and "timed out" in message
    assert (
        json.loads(slot.read_text())["claudeAiOauth"]["refreshToken"] == "new-refresh"
    )


def test_probe_refresh_does_not_overwrite_slot_reauthenticated_mid_probe(
    tmp_path: Path,
) -> None:
    # The operator re-authenticates the slot as account B while a probe that
    # started for account A is still running.
    slot = tmp_path / ".credentials.json.alice"
    _write_account_slot(slot, "account-a")
    reauth = (
        "slot = Path(" + repr(str(slot)) + ")\n"
        "fresh = json.loads(slot.read_text())\n"
        'fresh["claudeAiOauth"].update(accountUuid="account-b", refreshToken="b-refresh")\n'
        "slot.unlink()\n"
        "slot.write_text(json.dumps(fresh))"
    )
    script = _refresh_script(tmp_path, reauth)

    _, ok, message = probe_credential(slot, "alice", usage_script=script)
    assert ok, message

    oauth = json.loads(slot.read_text())["claudeAiOauth"]
    assert oauth["accountUuid"] == "account-b"
    assert oauth["refreshToken"] == "b-refresh"


def test_probe_refresh_does_not_overwrite_legacy_slot_reauthenticated_mid_probe(
    tmp_path: Path,
) -> None:
    # Same race for a slot written before Claude Code recorded accountUuid.
    slot = tmp_path / ".credentials.json.alice"
    _write_slot(slot)
    script = _refresh_script(
        tmp_path,
        "slot = Path(" + repr(str(slot)) + ")\n"
        "slot.unlink()\n"
        'slot.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "b-refresh", '
        '"accessToken": "b", "expiresAt": 1}}))',
    )

    _, ok, message = probe_credential(slot, "alice", usage_script=script)
    assert ok, message

    assert json.loads(slot.read_text())["claudeAiOauth"]["refreshToken"] == "b-refresh"
