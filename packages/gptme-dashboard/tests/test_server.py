"""Tests for the dynamic dashboard server."""

import json
import textwrap
from pathlib import Path

import pytest

from gptme_dashboard.server import create_app


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with gptme.toml and session data."""
    # gptme.toml
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "TestBot"
        """)
    )

    # Lessons dir (needed for generate)
    (tmp_path / "lessons").mkdir()

    # Session records
    sessions_dir = tmp_path / "state" / "sessions"
    sessions_dir.mkdir(parents=True)
    records = [
        {
            "session_id": "abc1",
            "timestamp": "2026-03-06T10:00:00Z",
            "harness": "claude-code",
            "model": "claude-opus-4-6",
            "run_type": "autonomous",
            "category": "code",
            "outcome": "productive",
            "duration_seconds": 600,
            "deliverables": [],
        },
        {
            "session_id": "abc2",
            "timestamp": "2026-03-06T11:00:00Z",
            "harness": "gptme",
            "model": "claude-sonnet-4-6",
            "run_type": "autonomous",
            "category": "triage",
            "outcome": "noop",
            "duration_seconds": 120,
            "deliverables": [],
        },
        {
            "session_id": "abc3",
            "timestamp": "2026-03-06T12:00:00Z",
            "harness": "claude-code",
            "model": "claude-opus-4-6",
            "run_type": "autonomous",
            "category": "infrastructure",
            "outcome": "productive",
            "duration_seconds": 900,
            "deliverables": ["commit:abc123"],
        },
    ]
    with open(sessions_dir / "session-records.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    return tmp_path


@pytest.fixture
def client(workspace: Path, tmp_path: Path):
    """Create Flask test client."""
    site_dir = tmp_path / "site"
    app = create_app(workspace, site_dir=site_dir)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_index_served(client):
    """Test that the static index.html is served at /."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"TestBot" in resp.data


def test_api_status(client):
    """Test /api/status returns dynamic mode info."""
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mode"] == "dynamic"
    assert data["agent"] == "TestBot"
    assert "workspace" in data


def test_api_sessions_stats(client):
    """Test /api/sessions/stats returns session statistics."""
    resp = client.get("/api/sessions/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 3
    assert data["productive"] == 2
    assert data["noop"] == 1
    assert 0 < data["success_rate"] < 1
    assert "by_model" in data
    assert "by_harness" in data


def test_api_sessions_stats_with_days(client):
    """Test /api/sessions/stats?days=N filters by recency."""
    # All sessions are from 2026-03-06, so days=1 from now (2026-03-06)
    # may or may not include them depending on timing. Just check it doesn't error.
    resp = client.get("/api/sessions/stats?days=365")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "total" in data


def test_api_sessions_stats_days_zero(client):
    """Test ?days=0 falls through to load_all (0 is not a valid positive filter)."""
    # days=0 is falsy in Python; ensure the guard `days is not None and days > 0`
    # treats it the same as no filter (load all sessions).
    resp = client.get("/api/sessions/stats?days=0")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 3  # all sessions returned, same as no filter


def test_api_sessions_list(client):
    """Test /api/sessions returns recent sessions."""
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) == 3
    # Most recent first
    assert data[0]["session_id"] == "abc3"
    assert data[0]["outcome"] == "productive"


def test_api_sessions_limit(client):
    """Test /api/sessions respects limit parameter."""
    resp = client.get("/api/sessions?limit=2")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 2


def test_api_sessions_limit_capped(client):
    """Test /api/sessions caps limit at 200."""
    resp = client.get("/api/sessions?limit=999")
    assert resp.status_code == 200
    # Should not error, just cap at 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_workspace_no_sessions(tmp_path: Path):
    """Test API gracefully handles missing session data."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Empty"\n')
    (tmp_path / "lessons").mkdir()

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/sessions/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 0

        resp = c.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.get_json() == []
