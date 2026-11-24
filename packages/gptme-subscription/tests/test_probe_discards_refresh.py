"""probe_credential runs the usage script against a throwaway HOME whose
.credentials.json is a symlink to the slot. Claude Code's token refresh
REPLACES that symlink with a regular file (documented in auth.REAUTH_INSTRUCTIONS),
so the rotated refresh token lands in the temp dir and is deleted with it.
The slot file keeps the already-consumed refresh token."""

from __future__ import annotations

import json
import time
from pathlib import Path

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
