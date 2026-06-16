"""Tests for scripts/subscription-token-probe.py.

Covers the live token-validity probe: API probe logic, TOKEN-DEAD marker
writes/removals, refresh-aware probing for stale/missing access tokens,
and the format_context/has_dead_slots helper functions.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROBE_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "scripts"
    / "subscription-token-probe.py"
)


def _load_probe():
    """Load subscription-token-probe.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "subscription_token_probe", PROBE_SCRIPT
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_cred_file(path: Path, expires_in_seconds: int = 3600) -> None:
    """Write a minimal valid credential file."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    expires_at_ms = now_ms + expires_in_seconds * 1000
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "fake-access-token-abc123",
                    "refreshToken": "fake-refresh-token-xyz789",
                    "expiresAt": expires_at_ms,
                    "subscriptionType": "claude_max",
                    "scopes": ["user:inference"],
                }
            }
        )
    )


def _make_http_response(status: int, body: bytes = b'{"input_tokens": 1}') -> MagicMock:
    """Return a mock HTTP response object."""
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---- _probe_slot ----


class TestProbeSlot:
    """Unit tests for _probe_slot(slot, token)."""

    def test_valid_200_returns_valid(self):
        """200 from the API → status='valid'."""
        mod = _load_probe()
        resp = _make_http_response(200)
        with patch.object(urllib.request, "urlopen", return_value=resp):
            result = mod._probe_slot("bob", "fake-token")
        assert result["status"] == "valid"
        assert result["slot"] == "bob"
        assert result["http_code"] == 200
        assert result["error"] is None

    def test_401_returns_dead(self):
        """401 → token is server-side dead."""
        mod = _load_probe()
        exc = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages/count_tokens",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"error": "invalid_token"}'),
        )
        with patch.object(urllib.request, "urlopen", side_effect=exc):
            result = mod._probe_slot("bob", "dead-token")
        assert result["status"] == "dead"
        assert result["http_code"] == 401

    def test_403_returns_dead(self):
        """403 → token is server-side dead."""
        mod = _load_probe()
        exc = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages/count_tokens",
            code=403,
            msg="Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"error": "forbidden"}'),
        )
        with patch.object(urllib.request, "urlopen", side_effect=exc):
            result = mod._probe_slot("alice", "bad-token")
        assert result["status"] == "dead"
        assert result["http_code"] == 403

    def test_429_returns_error_not_dead(self):
        """429 rate-limit → status='error', not 'dead' (token may still be alive)."""
        mod = _load_probe()
        exc = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages/count_tokens",
            code=429,
            msg="Too Many Requests",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"error": "rate_limited"}'),
        )
        with patch.object(urllib.request, "urlopen", side_effect=exc):
            result = mod._probe_slot("bob", "ok-token")
        assert result["status"] == "error"
        assert result["error"] == "rate_limited"

    def test_network_error_returns_error(self):
        """Network failure → status='error'."""
        mod = _load_probe()
        exc = urllib.error.URLError("connection refused")
        with patch.object(urllib.request, "urlopen", side_effect=exc):
            result = mod._probe_slot("bob", "some-token")
        assert result["status"] == "error"
        assert "connection" in (result["error"] or "").lower()

    def test_result_includes_checked_at(self):
        """Result always includes a checked_at ISO timestamp."""
        mod = _load_probe()
        resp = _make_http_response(200)
        with patch.object(urllib.request, "urlopen", return_value=resp):
            result = mod._probe_slot("bob", "fake-token")
        assert "checked_at" in result
        datetime.fromisoformat(result["checked_at"])


# ---- _offline_probe_error ----


class TestOfflineProbeError:
    """Unit tests for _offline_probe_error(slot) fast-reject path."""

    def test_fresh_token_probeable(self, tmp_path, monkeypatch):
        """A token expiring in 1 hour is probeable."""
        mod = _load_probe()
        cred = tmp_path / ".credentials.json.bob"
        _make_cred_file(cred, expires_in_seconds=3600)
        monkeypatch.setattr(mod, "CREDS_DIR", tmp_path)
        assert mod._offline_probe_error("bob") is None

    def test_expired_token_still_probeable(self, tmp_path, monkeypatch):
        """A lapsed access token is stale, not dead; live probe can refresh it."""
        mod = _load_probe()
        cred = tmp_path / ".credentials.json.bob"
        _make_cred_file(cred, expires_in_seconds=-300)
        monkeypatch.setattr(mod, "CREDS_DIR", tmp_path)
        assert mod._offline_probe_error("bob") is None

    def test_missing_file_returns_error(self, tmp_path, monkeypatch):
        """Missing credential file cannot be live-probed."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "CREDS_DIR", tmp_path)
        result = mod._offline_probe_error("noexist")
        assert result is not None
        assert result["status"] == "error"
        assert result["credential_status"] == "missing"


# ---- _write_dead_markers ----


class TestWriteDeadMarkers:
    """Unit tests for _write_dead_markers(results)."""

    def test_noop_when_dead_slot_dir_is_none(self, tmp_path, monkeypatch):
        """When DEAD_SLOT_DIR is None, _write_dead_markers is a no-op."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", None)
        results = [{"slot": "bob", "status": "dead", "checked_at": "t"}]
        mod._write_dead_markers(results)
        # No exception, no files written elsewhere

    def test_creates_marker_for_dead_slot(self, tmp_path, monkeypatch):
        """Dead slot → TOKEN-DEAD-<slot> marker file is created."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", tmp_path)
        results = [
            {"slot": "bob", "status": "dead", "checked_at": "2026-01-01T00:00:00"}
        ]
        mod._write_dead_markers(results)
        marker = tmp_path / "slot-TOKEN-DEAD-bob"
        assert marker.exists(), "TOKEN-DEAD marker should exist for dead slot"

    def test_no_marker_for_valid_slot(self, tmp_path, monkeypatch):
        """Valid slot → no TOKEN-DEAD marker."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", tmp_path)
        results = [
            {"slot": "alice", "status": "valid", "checked_at": "2026-01-01T00:00:00"}
        ]
        mod._write_dead_markers(results)
        marker = tmp_path / "slot-TOKEN-DEAD-alice"
        assert not marker.exists()

    def test_clears_stale_markers_on_recovery(self, tmp_path, monkeypatch):
        """When a previously dead slot recovers, its stale marker is removed."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", tmp_path)

        stale_marker = tmp_path / "slot-TOKEN-DEAD-bob"
        stale_marker.write_text("stale dead marker\n")

        results = [
            {"slot": "bob", "status": "valid", "checked_at": "2026-01-01T00:00:00"}
        ]
        mod._write_dead_markers(results)

        assert not stale_marker.exists(), "Stale marker should be cleared on recovery"

    def test_mixed_results(self, tmp_path, monkeypatch):
        """Dead slot gets marker; valid slot does not; stale marker cleared."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", tmp_path)

        (tmp_path / "slot-TOKEN-DEAD-alice").write_text("old\n")

        results = [
            {"slot": "alice", "status": "valid", "checked_at": "t"},
            {"slot": "erik", "status": "dead", "checked_at": "t"},
        ]
        mod._write_dead_markers(results)

        assert not (tmp_path / "slot-TOKEN-DEAD-alice").exists()
        assert (tmp_path / "slot-TOKEN-DEAD-erik").exists()

    def test_non_token_dead_files_untouched(self, tmp_path, monkeypatch):
        """Files in DEAD_SLOT_DIR that don't start with 'slot-TOKEN-DEAD-' are preserved."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", tmp_path)

        other = tmp_path / "some-other-marker.txt"
        other.write_text("unrelated\n")

        mod._write_dead_markers([{"slot": "bob", "status": "valid", "checked_at": "t"}])

        assert other.exists(), "Non-TOKEN-DEAD files must not be removed"


# ---- probe_all ----


class TestProbeAll:
    """Integration tests for probe_all()."""

    def test_skips_active_slot(self, tmp_path, monkeypatch):
        """probe_all never probes the slot the live symlink points to."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", None)
        monkeypatch.setattr(mod, "CREDS_DIR", tmp_path / "creds")
        (tmp_path / "creds").mkdir()

        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")

        probed: list[str] = []

        def fake_probe(slot: str) -> dict:
            probed.append(slot)
            return {
                "slot": slot,
                "status": "valid",
                "http_code": 200,
                "checked_at": "t",
                "error": None,
            }

        monkeypatch.setattr(mod, "_offline_probe_error", lambda s: None)
        monkeypatch.setattr(mod, "_probe_slot_with_refresh", fake_probe)

        out = tmp_path / "health.json"
        mod.probe_all(slots=["bob", "alice", "erik"], output=out)

        assert "bob" not in probed, "Active slot must not be probed"
        assert "alice" in probed
        assert "erik" in probed

    def test_writes_state_file(self, tmp_path, monkeypatch):
        """probe_all writes results to the output JSON file."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", None)
        monkeypatch.setattr(mod, "CREDS_DIR", tmp_path / "creds")
        (tmp_path / "creds").mkdir()

        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")
        monkeypatch.setattr(mod, "_offline_probe_error", lambda s: None)
        monkeypatch.setattr(
            mod,
            "_probe_slot_with_refresh",
            lambda slot: {
                "slot": slot,
                "status": "valid",
                "http_code": 200,
                "checked_at": "t",
                "error": None,
            },
        )

        out = tmp_path / "health.json"
        mod.probe_all(slots=["bob", "alice"], output=out)

        assert out.exists()
        data = json.loads(out.read_text())
        assert isinstance(data, list)
        slots_in_file = {r["slot"] for r in data}
        assert "alice" in slots_in_file

    def test_dead_slot_creates_marker(self, tmp_path, monkeypatch):
        """When probe_all finds a dead slot, the TOKEN-DEAD marker is written."""
        mod = _load_probe()
        quota_dir = tmp_path / "quota"
        quota_dir.mkdir()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", quota_dir)
        monkeypatch.setattr(mod, "CREDS_DIR", tmp_path / "creds")
        (tmp_path / "creds").mkdir()

        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")
        monkeypatch.setattr(mod, "_offline_probe_error", lambda s: None)

        def dead_probe(slot: str) -> dict:
            return {
                "slot": slot,
                "status": "dead",
                "http_code": 401,
                "checked_at": "t",
                "error": "HTTP 401",
            }

        monkeypatch.setattr(mod, "_probe_slot_with_refresh", dead_probe)

        out = tmp_path / "health.json"
        mod.probe_all(slots=["bob", "alice"], output=out)

        assert (quota_dir / "slot-TOKEN-DEAD-alice").exists()

    def test_stale_access_tokens_are_live_probed(self, tmp_path, monkeypatch):
        """Slots with lapsed access tokens still use the refresh-aware probe."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", None)
        creds = tmp_path / "creds"
        monkeypatch.setattr(mod, "CREDS_DIR", creds)
        creds.mkdir()
        _make_cred_file(creds / ".credentials.json.alice", expires_in_seconds=-300)
        _make_cred_file(creds / ".credentials.json.erik", expires_in_seconds=-300)

        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")

        probed: list[str] = []

        def fake_probe(slot: str) -> dict:
            probed.append(slot)
            return {
                "slot": slot,
                "status": "valid",
                "http_code": 200,
                "checked_at": "t",
                "error": None,
            }

        monkeypatch.setattr(mod, "_probe_slot_with_refresh", fake_probe)

        out = tmp_path / "health.json"
        mod.probe_all(slots=["bob", "alice", "erik"], output=out)

        assert probed == ["alice", "erik"]

    def test_missing_access_token_still_uses_refresh_probe(self, tmp_path, monkeypatch):
        """A parseable slot can still live-probe through refresh without accessToken."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "DEAD_SLOT_DIR", None)
        creds = tmp_path / "creds"
        monkeypatch.setattr(mod, "CREDS_DIR", creds)
        creds.mkdir()
        (creds / ".credentials.json.alice").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "refreshToken": "fake-refresh-token-xyz789",
                        "expiresAt": int(
                            (
                                datetime.now(timezone.utc) + timedelta(hours=1)
                            ).timestamp()
                            * 1000
                        ),
                        "subscriptionType": "claude_max",
                    }
                }
            )
        )

        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")

        info = SimpleNamespace(
            status="valid",
            error=None,
            expires_in_seconds=3600,
            subscription_type="claude_max",
        )

        monkeypatch.setattr(mod, "check_credential_file", lambda path, slot: info)
        probed: list[str] = []

        def fake_probe_credential(path, slot, usage_script=None, timeout=None):
            probed.append(slot)
            return info, True, "ok"

        monkeypatch.setattr(mod, "probe_credential", fake_probe_credential)

        out = tmp_path / "health.json"
        mod.probe_all(slots=["bob", "alice"], output=out)

        assert (
            "alice" in probed
        ), "alice should be live-probed despite missing accessToken"


# ---- format_context / has_dead_slots ----


class TestHelpers:
    """Unit tests for format_context and has_dead_slots."""

    def test_format_context_no_data(self):
        mod = _load_probe()
        assert mod.format_context([]) == "token-probe: no data"

    def test_format_context_valid_slots(self):
        mod = _load_probe()
        results = [
            {"slot": "alice", "status": "valid", "checked_at": "2026-01-01T10:00:00"},
            {"slot": "erik", "status": "valid", "checked_at": "2026-01-01T10:00:00"},
        ]
        ctx = mod.format_context(results)
        assert "alice=ok" in ctx
        assert "erik=ok" in ctx

    def test_format_context_dead_slot(self):
        mod = _load_probe()
        results = [
            {"slot": "alice", "status": "dead", "checked_at": "2026-01-01T10:00:00"},
        ]
        ctx = mod.format_context(results)
        assert "alice=DEAD(" in ctx

    def test_has_dead_slots_true(self):
        mod = _load_probe()
        assert mod.has_dead_slots([{"slot": "alice", "status": "dead"}])

    def test_has_dead_slots_false(self):
        mod = _load_probe()
        assert not mod.has_dead_slots([{"slot": "alice", "status": "valid"}])
