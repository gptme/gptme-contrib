"""Regressions from the 2026-11-24 verified bug hunt (synthetic fixtures only)."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from credential_slots import SlotManager


def _ms(offset_s: float) -> int:
    return int((datetime.now(timezone.utc).timestamp() + offset_s) * 1000)


def _cred(rt: str, expires_ms: int) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": f"at-{rt}",
                "refreshToken": rt,
                "expiresAt": expires_ms,
            }
        }
    )


@pytest.fixture
def mgr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SlotManager:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = tmp_path / "creds"
    d.mkdir()
    return SlotManager(creds_dir=d, subscriptions=["bob", "alice"])


def test_heal_slot_temp_is_never_group_or_world_readable(
    mgr: SlotManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr.slot_path("bob").write_text(_cred("rt-old", _ms(-3600)))
    mgr.live_path.write_text(_cred("rt-new", _ms(3600)))
    os.chmod(mgr.live_path, 0o644)
    # The temp file must be private from creation, not after a later chmod
    # (copy2-then-chmod exposed it at 0o644 under umask 022).
    monkeypatch.setattr(os, "chmod", lambda *a, **kw: None)
    modes: list[int] = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        if Path(dst) == mgr.slot_path("bob"):
            modes.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    old_umask = os.umask(0o022)
    try:
        result = mgr.heal_drift_to("bob")
    finally:
        os.umask(old_umask)
    assert result.ok, result.reason
    assert modes == [0o600]


def test_heal_installs_the_bytes_it_validated(
    mgr: SlotManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer rewriting live after heal's validation must not get its
    partial bytes installed as the slot."""
    mgr.slot_path("bob").write_text(_cred("rt-old", _ms(-3600)))
    good = _cred("rt-new", _ms(3600))
    mgr.live_path.write_text(good)
    real_open = os.open
    fired: list[bool] = []

    def racing_open(path, flags, *a, **kw):
        if not fired and flags & os.O_CREAT and ".tmp" in str(path):
            fired.append(True)
            mgr.live_path.write_text(good[:20])  # concurrent writer mid-rewrite
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", racing_open)
    result = mgr.heal_drift_to("bob")
    assert result.ok, result.reason
    assert mgr.slot_path("bob").read_text() == good


def test_nested_switch_in_same_process_does_not_collide(
    mgr: SlotManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second switch_to in the same process (another thread) between the
    first call's symlink_to and os.replace used to hit FileExistsError and
    unlink the first call's temp link."""
    mgr.slot_path("bob").write_text(_cred("rt-bob", _ms(3600)))
    mgr.slot_path("alice").write_text(_cred("rt-alice", _ms(3600)))
    mgr.live_path.symlink_to(".credentials.json.bob")
    real_replace = os.replace
    nested: dict[str, object] = {}

    def interleave(src, dst, *a, **kw):
        if not nested:
            nested["started"] = True
            nested["result"] = mgr.switch_to("bob", "thread B")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", interleave)
    outer = mgr.switch_to("alice", "thread A")
    assert getattr(nested["result"], "ok") is True
    assert outer.ok
    assert os.readlink(mgr.live_path) == ".credentials.json.alice"
    assert not [p for p in mgr.creds_dir.iterdir() if ".tmp" in p.name]
