"""Tests for scripts/subscription-token-probe.py.

Covers live token-validity probe logic: API probe, stale-token handling,
TOKEN-DEAD marker management, and probe_all integration.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

PROBE_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "subscription-token-probe.py"
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
    """Unit tests for _write_dead_markers(results, dead_slot_dir)."""

    def test_creates_marker_for_dead_slot(self, tmp_path):
        """Dead slot → TOKEN-DEAD-<slot> marker file is created."""
        mod = _load_probe()
        results = [
            {"slot": "bob", "status": "dead", "checked_at": "2026-01-01T00:00:00"}
        ]
        mod._write_dead_markers(results, tmp_path)
        marker = tmp_path / "slot-TOKEN-DEAD-bob"
        assert marker.exists(), "TOKEN-DEAD marker should exist for dead slot"

    def test_no_marker_for_valid_slot(self, tmp_path):
        """Valid slot → no TOKEN-DEAD marker."""
        mod = _load_probe()
        results = [
            {"slot": "alice", "status": "valid", "checked_at": "2026-01-01T00:00:00"}
        ]
        mod._write_dead_markers(results, tmp_path)
        marker = tmp_path / "slot-TOKEN-DEAD-alice"
        assert not marker.exists()

    def test_clears_stale_markers_on_recovery(self, tmp_path):
        """When a previously dead slot recovers, its stale marker is removed."""
        mod = _load_probe()

        stale_marker = tmp_path / "slot-TOKEN-DEAD-bob"
        stale_marker.write_text("stale dead marker\n")

        results = [
            {"slot": "bob", "status": "valid", "checked_at": "2026-01-01T00:00:00"}
        ]
        mod._write_dead_markers(results, tmp_path)

        assert not stale_marker.exists(), "Stale marker should be cleared on recovery"

    def test_mixed_results(self, tmp_path):
        """Dead slot gets marker; valid slot does not; stale marker for third cleared."""
        mod = _load_probe()

        (tmp_path / "slot-TOKEN-DEAD-alice").write_text("old\n")

        results = [
            {"slot": "alice", "status": "valid", "checked_at": "t"},
            {"slot": "erik", "status": "dead", "checked_at": "t"},
        ]
        mod._write_dead_markers(results, tmp_path)

        assert not (tmp_path / "slot-TOKEN-DEAD-alice").exists()
        assert (tmp_path / "slot-TOKEN-DEAD-erik").exists()

    def test_non_token_dead_files_untouched(self, tmp_path):
        """Files in dead_slot_dir that don't start with 'slot-TOKEN-DEAD-' are preserved."""
        mod = _load_probe()

        other = tmp_path / "some-other-marker.txt"
        other.write_text("unrelated\n")

        mod._write_dead_markers(
            [{"slot": "bob", "status": "valid", "checked_at": "t"}], tmp_path
        )

        assert other.exists(), "Non-TOKEN-DEAD files must not be removed"

    def test_noop_when_dir_is_none(self, tmp_path):
        """When dead_slot_dir is None, no files are created."""
        mod = _load_probe()
        mod._write_dead_markers(
            [{"slot": "bob", "status": "dead", "checked_at": "t"}], None
        )
        # Just confirm no exception and no side effects


# ---- probe_all ----


class TestProbeAll:
    """Integration tests for probe_all()."""

    def test_skips_active_slot(self, tmp_path, monkeypatch):
        """probe_all never probes the slot the live symlink points to."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")

        probed: list[str] = []

        def fake_probe(slot: str, usage_script=None) -> dict:
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
        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")
        monkeypatch.setattr(mod, "_offline_probe_error", lambda s: None)
        monkeypatch.setattr(
            mod,
            "_probe_slot_with_refresh",
            lambda slot, usage_script=None: {
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

    def test_no_output_file_when_output_is_none(self, tmp_path, monkeypatch):
        """probe_all returns results without writing to disk when output=None."""
        mod = _load_probe()
        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")
        monkeypatch.setattr(mod, "_offline_probe_error", lambda s: None)
        monkeypatch.setattr(
            mod,
            "_probe_slot_with_refresh",
            lambda slot, usage_script=None: {
                "slot": slot,
                "status": "valid",
                "checked_at": "t",
                "error": None,
            },
        )

        results = mod.probe_all(slots=["bob", "alice"], output=None)
        assert isinstance(results, list)
        assert len(results) == 1  # only alice (bob is active)

    def test_dead_slot_creates_marker(self, tmp_path, monkeypatch):
        """When probe_all finds a dead slot, the TOKEN-DEAD marker is written."""
        mod = _load_probe()
        quota_dir = tmp_path / "quota"
        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")
        monkeypatch.setattr(mod, "_offline_probe_error", lambda s: None)

        def dead_probe(slot: str, usage_script=None) -> dict:
            return {
                "slot": slot,
                "status": "dead",
                "http_code": 401,
                "checked_at": "t",
                "error": "HTTP 401",
            }

        monkeypatch.setattr(mod, "_probe_slot_with_refresh", dead_probe)

        out = tmp_path / "health.json"
        mod.probe_all(slots=["bob", "alice"], output=out, dead_slot_dir=quota_dir)

        assert (quota_dir / "slot-TOKEN-DEAD-alice").exists()

    def test_stale_access_tokens_are_live_probed(self, tmp_path, monkeypatch):
        """Slots with lapsed access tokens still use the refresh-aware probe."""
        mod = _load_probe()
        creds = tmp_path / "creds"
        monkeypatch.setattr(mod, "CREDS_DIR", creds)
        creds.mkdir()
        _make_cred_file(creds / ".credentials.json.alice", expires_in_seconds=-300)
        _make_cred_file(creds / ".credentials.json.erik", expires_in_seconds=-300)

        monkeypatch.setattr(mod, "_active_slot", lambda: "bob")

        probed: list[str] = []

        def fake_probe(slot: str, usage_script=None) -> dict:
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
