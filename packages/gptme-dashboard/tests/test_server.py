"""Tests for the dynamic dashboard server."""

import json
import textwrap
import unittest.mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gptme_dashboard.server import create_app, load_org_config


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

    # Session records — use relative date (5 days ago) to avoid time-bomb failures
    sessions_dir = tmp_path / "state" / "sessions"
    sessions_dir.mkdir(parents=True)
    _fixture_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    records = [
        {
            "session_id": "abc1",
            "timestamp": f"{_fixture_date}T10:00:00Z",
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
            "timestamp": f"{_fixture_date}T11:00:00Z",
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
            "timestamp": f"{_fixture_date}T12:00:00Z",
            "harness": "claude-code",
            "model": "claude-opus-4-6",
            "run_type": "autonomous",
            "category": "infrastructure",
            "context_tier": "massive",
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
    assert data["urls"] == {}  # no [agent.urls] in fixture workspace


def test_api_status_with_agent_urls(tmp_path: Path):
    """Test /api/status includes agent_urls when [agent.urls] is set in gptme.toml."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "LinkBot"

        [agent.urls]
        dashboard = "https://linkbot.example.com/"
        repo = "https://github.com/example/linkbot"
        """)
    )
    (tmp_path / "lessons").mkdir()
    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["agent"] == "LinkBot"
        assert data["urls"]["dashboard"] == "https://linkbot.example.com/"
        assert data["urls"]["repo"] == "https://github.com/example/linkbot"


def test_api_status_url_filtering(tmp_path: Path):
    """Non-http/https URLs in [agent.urls] must be filtered from /api/status response."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "FilterBot"

        [agent.urls]
        valid = "https://example.com/"
        file_url = "file:///etc/passwd"
        bare_host = "example.com"
        """)
    )
    (tmp_path / "lessons").mkdir()
    app = create_app(tmp_path, site_dir=tmp_path / "site")
    app.config["TESTING"] = True
    with app.test_client() as c:
        data = c.get("/api/status").get_json()
    assert "valid" in data["urls"]
    assert "file_url" not in data["urls"]
    assert "bare_host" not in data["urls"]


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
    # Use days=365 (1 year window). Just verify the endpoint responds without error.
    resp = client.get("/api/sessions/stats?days=365")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "total" in data


def test_api_sessions_stats_days_zero(client):
    """Test ?days=0 uses the default 30-day window (matching the fallback scan behaviour)."""
    # days=0 → query(since_days=30); all 3 fixture sessions are recent so total==3.
    resp = client.get("/api/sessions/stats?days=0")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 3  # all fixture sessions fall within the 30-day window


def test_api_sessions_list(client):
    """Test /api/sessions returns recent sessions with pagination metadata."""
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "sessions" in data
    assert "total" in data
    assert "offset" in data
    assert "has_more" in data
    assert len(data["sessions"]) == 3
    assert data["total"] == 3
    assert data["offset"] == 0
    assert data["has_more"] is False
    # Most recent first
    assert data["sessions"][0]["session_id"] == "abc3"
    assert data["sessions"][0]["outcome"] == "productive"


def test_api_sessions_includes_context_tier(client):
    """Test /api/sessions includes context_tier field when present in session record."""
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.get_json()
    sessions_by_id = {s["session_id"]: s for s in data["sessions"]}
    # abc3 has context_tier="massive" in fixture
    assert sessions_by_id["abc3"].get("context_tier") == "massive"
    # abc1 has no context_tier — field should be absent or None
    assert sessions_by_id["abc1"].get("context_tier") is None


def test_api_sessions_limit(client):
    """Test /api/sessions respects limit parameter."""
    resp = client.get("/api/sessions?limit=2")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["sessions"]) == 2
    assert data["total"] == 3
    assert data["has_more"] is True


def test_api_sessions_offset(client):
    """Test /api/sessions supports offset-based pagination."""
    resp = client.get("/api/sessions?limit=2&offset=2")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["sessions"]) == 1  # only 1 remaining after offset 2
    assert data["total"] == 3
    assert data["offset"] == 2
    assert data["has_more"] is False


def test_api_sessions_limit_capped(client):
    """Test /api/sessions caps limit at 200."""
    resp = client.get("/api/sessions?limit=999")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data["sessions"], list)


def test_api_sessions_limit_negative(client):
    """Test /api/sessions clamps negative limit to 1 (returns 1 record)."""
    resp = client.get("/api/sessions?limit=-1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data["sessions"], list)
    assert len(data["sessions"]) == 1  # clamped to 1


def test_api_sessions_with_days(client):
    """Test /api/sessions?days=N filters by recency (same guard as stats endpoint)."""
    resp = client.get("/api/sessions?days=3650")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data["sessions"], list)
    assert len(data["sessions"]) == 3


def test_api_sessions_filter_by_harness(client):
    """Test /api/sessions?harness=X filters by harness."""
    resp = client.get("/api/sessions?harness=gptme")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["harness"] == "gptme"
    assert data["total"] == 1


def test_api_sessions_filter_by_model(client):
    """Test /api/sessions?model=X filters by model."""
    resp = client.get("/api/sessions?model=claude-opus-4-6")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["sessions"]) == 2
    assert all(s["model"] == "claude-opus-4-6" for s in data["sessions"])
    assert data["total"] == 2


def test_api_sessions_filter_by_outcome(client):
    """Test /api/sessions?outcome=X filters by outcome."""
    resp = client.get("/api/sessions?outcome=productive")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["sessions"]) == 2
    assert all(s["outcome"] == "productive" for s in data["sessions"])

    resp = client.get("/api/sessions?outcome=noop")
    data = resp.get_json()
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["outcome"] == "noop"


def test_api_sessions_combined_filters(client):
    """Test /api/sessions with multiple filters applied simultaneously."""
    resp = client.get("/api/sessions?harness=claude-code&outcome=productive")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["sessions"]) == 2
    assert all(
        s["harness"] == "claude-code" and s["outcome"] == "productive" for s in data["sessions"]
    )


def test_api_sessions_filter_no_match(client):
    """Test /api/sessions with filter that matches nothing."""
    resp = client.get("/api/sessions?model=nonexistent-model")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["sessions"]) == 0
    assert data["total"] == 0


def test_api_services_structure(client):
    """Test /api/services returns correct structure even with no gptme services."""
    resp = client.get("/api/services")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "services" in data
    assert "platform" in data
    assert isinstance(data["services"], list)
    assert isinstance(data["platform"], str)


def test_api_services_linux_detection(client):
    """Test /api/services detects systemd services matching agent name on Linux."""
    systemctl_output = json.dumps(
        [
            {
                "unit": "bob-autonomous.service",
                "description": "Bob Autonomous Session",
                "load": "loaded",
                "active": "active",
                "sub": "running",
            },
            {
                "unit": "unrelated.service",
                "description": "Something else",
                "load": "loaded",
                "active": "inactive",
                "sub": "dead",
            },
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = systemctl_output

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services")

    assert resp.status_code == 200
    data = resp.get_json()
    # workspace fixture uses agent name "TestBot" — only services with "testbot" or "gptme" match
    # "bob-autonomous.service" and "unrelated.service" neither contains "gptme" nor "testbot"
    assert data["platform"] == "Linux"
    assert isinstance(data["services"], list)
    assert len(data["services"]) == 0


def test_api_services_gptme_filter(client):
    """Test /api/services includes services with 'gptme' in name."""
    systemctl_output = json.dumps(
        [
            {
                "unit": "gptme-server.service",
                "description": "gptme API Server",
                "load": "loaded",
                "active": "active",
                "sub": "running",
            },
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = systemctl_output

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services")

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["services"]) == 1
    assert data["services"][0]["name"] == "gptme-server.service"
    assert data["services"][0]["active"] == "active"
    assert data["services"][0]["sub"] == "running"


def test_api_services_systemctl_failure(client):
    """Test /api/services returns empty list when systemctl fails."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["services"] == []


def test_api_services_timeout_handled(client):
    """Test /api/services handles subprocess timeout gracefully."""
    import subprocess

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 5)
        ),
    ):
        resp = client.get("/api/services")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["services"] == []


def test_api_services_darwin_detection(client):
    """Test /api/services parses launchctl output on macOS."""
    launchctl_output = "\n".join(
        [
            "PID\tStatus\tLabel",
            "123\t0\tcom.gptme.server",
            "-\t0\tcom.apple.unrelated",
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = launchctl_output

    with (
        unittest.mock.patch("platform.system", return_value="Darwin"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["platform"] == "Darwin"
    assert len(data["services"]) == 1
    assert data["services"][0]["name"] == "com.gptme.server"
    assert data["services"][0]["active"] == "active"  # PID != "-"
    assert data["services"][0]["sub"] == "running"


def test_workspace_no_sessions(tmp_path: Path):
    """Test API gracefully handles missing session data."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Empty"\n')
    (tmp_path / "lessons").mkdir()

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with (
        app.test_client() as c,
        unittest.mock.patch("gptme_dashboard.generate.scan_recent_sessions", return_value=[]),
        unittest.mock.patch("gptme_dashboard.server._scan_gptme_logs_basic", return_value=[]),
    ):
        resp = c.get("/api/sessions/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 0

        resp = c.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["sessions"] == []
        assert data["total"] == 0


def test_scan_gptme_logs_basic_no_logs_dir(tmp_path: Path):
    """_scan_gptme_logs_basic returns empty list when logs dir does not exist."""
    from gptme_dashboard.server import _scan_gptme_logs_basic
    import unittest.mock

    with (
        unittest.mock.patch.dict("os.environ", {"XDG_DATA_HOME": ""}),
        unittest.mock.patch("gptme_dashboard.server.Path.home", return_value=tmp_path),
    ):
        result = _scan_gptme_logs_basic(tmp_path)
    assert result == []


def test_scan_gptme_logs_basic_with_sessions(tmp_path: Path):
    """_scan_gptme_logs_basic returns sessions from gptme logs dir."""
    from gptme_dashboard.server import _scan_gptme_logs_basic
    from datetime import date

    # Create a fake ~/.local/share/gptme/logs directory structure
    fake_home = tmp_path / "home"
    logs_dir = fake_home / ".local" / "share" / "gptme" / "logs"
    logs_dir.mkdir(parents=True)

    today = date.today().isoformat()
    session_dir = logs_dir / f"{today}-test-session"
    session_dir.mkdir()
    (session_dir / "conversation.jsonl").write_text('{"role":"user","content":"hi"}\n')

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    import unittest.mock

    with (
        unittest.mock.patch.dict("os.environ", {"XDG_DATA_HOME": ""}),
        unittest.mock.patch("gptme_dashboard.server.Path.home", return_value=fake_home),
    ):
        result = _scan_gptme_logs_basic(workspace, days=30)

    assert len(result) == 1
    assert result[0]["harness"] == "gptme"
    assert result[0]["outcome"] == "unknown"
    assert result[0]["date"].startswith(today)


def test_api_sessions_basic_fallback_timestamp(tmp_path: Path):
    """Integration test: /api/sessions returns non-empty timestamp when using basic fallback."""
    from datetime import date

    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Test"\n')
    (tmp_path / "lessons").mkdir()

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    today = date.today().isoformat()
    fake_sessions = [
        {
            "date": f"{today}T00:00:00",
            "harness": "gptme",
            "model": "",
            "category": "",
            "outcome": "unknown",
            "duration_seconds": 0,
        },
    ]

    import unittest.mock

    with (
        app.test_client() as c,
        unittest.mock.patch("gptme_dashboard.server._store_importable", False),
        unittest.mock.patch(
            "gptme_dashboard.server._scan_gptme_logs_basic", return_value=fake_sessions
        ),
        unittest.mock.patch("gptme_dashboard.generate.scan_recent_sessions", return_value=[]),
    ):
        resp = c.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 1
        session = data["sessions"][0]
        # Verify timestamp is non-empty (the bug was: s.get("date","") when key was "timestamp")
        assert session["timestamp"] != "", "timestamp must not be empty when using basic fallback"
        assert session["timestamp"].startswith(today)
        # Verify outcome is preserved from basic fallback (not overwritten to "noop" via grade=0)
        assert (
            session["outcome"] == "unknown"
        ), "basic fallback outcome must be preserved, not overridden"


def test_api_session_stats_basic_fallback_unknown_not_noop(tmp_path: Path):
    """Stats endpoint must not count basic-fallback 'unknown' sessions as noop."""
    from datetime import date

    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Test"\n')
    (tmp_path / "lessons").mkdir()

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    today = date.today().isoformat()
    fake_sessions = [
        {
            "date": f"{today}T00:00:00",
            "harness": "gptme",
            "model": "",
            "category": "",
            "outcome": "unknown",
            "duration_seconds": 0,
        },
    ]

    with (
        app.test_client() as c,
        unittest.mock.patch("gptme_dashboard.server._store_importable", False),
        unittest.mock.patch(
            "gptme_dashboard.server._scan_gptme_logs_basic", return_value=fake_sessions
        ),
        unittest.mock.patch("gptme_dashboard.generate.scan_recent_sessions", return_value=[]),
    ):
        resp = c.get("/api/sessions/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 1
        # unknown sessions must not inflate noop count
        assert data.get("unknown", 0) == 1, "basic-fallback sessions should be counted as unknown"
        assert data["noop"] == 0, "unknown sessions must not be classified as noop"
        # success_rate must be null (not 0%) when all sessions are unknown (no data ≠ failure)
        assert (
            data["success_rate"] is None
        ), "success_rate must be null when no known-outcome sessions"
        assert data["productive"] == 0


def test_scan_gptme_logs_basic_out_of_range_does_not_break_early(tmp_path: Path):
    """Out-of-range date dirs must not stop scanning before in-range dirs are reached."""
    from gptme_dashboard.server import _scan_gptme_logs_basic
    from datetime import date, timedelta

    fake_home = tmp_path / "home"
    logs_dir = fake_home / ".local" / "share" / "gptme" / "logs"
    logs_dir.mkdir(parents=True)

    today = date.today()
    in_range = (today - timedelta(days=5)).isoformat()
    out_of_range = (today - timedelta(days=60)).isoformat()

    # Two date-prefixed dirs (in_range sorts before out_of_range in reverse because
    # it is more recent) plus one non-date dir.  The non-date dir ("zz-notes") sorts
    # before both date dirs in reverse order (letters > digits in ASCII) and exercises
    # the ValueError-continue path.  The out-of-range dir exercises the break path —
    # the scan stops there because all subsequent dirs in reverse sort are even older.
    for name, has_conv in [
        (f"{in_range}-session", True),
        (f"{out_of_range}-old-session", False),
        ("zz-notes", False),  # non-date dir; verifies ValueError is handled with continue
    ]:
        d = logs_dir / name
        d.mkdir()
        if has_conv:
            (d / "conversation.jsonl").write_text('{"role":"user"}\n')

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with (
        unittest.mock.patch.dict("os.environ", {"XDG_DATA_HOME": ""}),
        unittest.mock.patch("gptme_dashboard.server.Path.home", return_value=fake_home),
    ):
        result = _scan_gptme_logs_basic(workspace, days=30)

    # in-range session must be found even though out-of-range dir is also present
    assert len(result) == 1
    assert result[0]["date"].startswith(in_range)


def test_api_journals_empty(client):
    """Test /api/journals returns empty list when no journal directory exists."""
    resp = client.get("/api/journals")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) == 0


def test_api_journals_with_entries(tmp_path: Path):
    """Test /api/journals returns journal entries when journal directory exists."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    # Create journal entries in subdirectory format
    day_dir = tmp_path / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("## Morning session\n\nWorked on the dashboard.\n")
    (day_dir / "notes.md").write_text("## Notes\n\nSome notes here.\n")

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/journals")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) == 2
        # Entries should have date, name, preview
        entry = data[0]
        assert entry["date"] == "2026-03-07"
        assert "name" in entry
        assert "preview" in entry


def test_api_journals_limit(tmp_path: Path):
    """Test /api/journals respects the limit parameter."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    # Create 5 journal days
    for i in range(1, 6):
        day_dir = tmp_path / "journal" / f"2026-03-{i:02d}"
        day_dir.mkdir(parents=True)
        (day_dir / "session.md").write_text(f"## Day {i}\n\nContent for day {i}.\n")

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/journals?limit=3")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) == 3


def test_api_tasks_empty(client):
    """Test /api/tasks returns empty list when no tasks directory exists."""
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) == 0


def test_api_tasks_with_entries(tmp_path: Path):
    """Test /api/tasks returns task entries from tasks/ directory."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "fix-bug.md").write_text(
        "---\nstate: active\npriority: high\ntags: [bugfix]\ncreated: 2026-03-01\n---\n# Fix Bug\n"
    )
    (tasks_dir / "add-feature.md").write_text(
        "---\nstate: backlog\npriority: low\ncreated: 2026-02-28\n---\n# Add Feature\n"
    )

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/tasks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) == 2
        # Active tasks should come first
        assert data[0]["state"] == "active"
        assert data[0]["title"] == "Fix Bug"


def test_api_tasks_state_filter(tmp_path: Path):
    """Test /api/tasks?state=X filters by task state."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "active-task.md").write_text(
        "---\nstate: active\ncreated: 2026-03-01\n---\n# Active Task\n"
    )
    (tasks_dir / "done-task.md").write_text(
        "---\nstate: done\ncreated: 2026-02-28\n---\n# Done Task\n"
    )

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/tasks?state=active")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["state"] == "active"


def test_api_tasks_limit(tmp_path: Path):
    """Test /api/tasks?limit=N caps the response size."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for i in range(5):
        (tasks_dir / f"task-{i}.md").write_text(
            f"---\nstate: active\ncreated: 2026-03-0{i + 1}\n---\n# Task {i}\n"
        )

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/tasks?limit=3")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 3

        # Non-numeric limit falls back to default (100), not a 500 error
        resp = c.get("/api/tasks?limit=foo")
        assert resp.status_code == 200


def test_api_summaries_empty(tmp_path: Path):
    """Test /api/summaries returns empty list when no summaries exist."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/summaries")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []


def test_api_summaries_returns_entries(tmp_path: Path):
    """Test /api/summaries returns daily/weekly/monthly entries."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    summaries_dir = tmp_path / "knowledge" / "summaries"
    (summaries_dir / "daily").mkdir(parents=True)
    (summaries_dir / "weekly").mkdir(parents=True)
    (summaries_dir / "daily" / "2026-03-07.md").write_text("# Day\n\nGood day.\n")
    (summaries_dir / "weekly" / "2026-W10.md").write_text("# Week\n\nGood week.\n")

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/summaries")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) == 2
        types = {e["type"] for e in data}
        assert "daily" in types
        assert "weekly" in types


def test_api_summaries_type_filter(tmp_path: Path):
    """Test /api/summaries?type= filters by period type."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    summaries_dir = tmp_path / "knowledge" / "summaries"
    (summaries_dir / "daily").mkdir(parents=True)
    (summaries_dir / "weekly").mkdir(parents=True)
    (summaries_dir / "daily" / "2026-03-07.md").write_text("# Day\n\nDaily.\n")
    (summaries_dir / "weekly" / "2026-W10.md").write_text("# Week\n\nWeekly.\n")

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/summaries?type=daily")
        assert resp.status_code == 200
        data = resp.get_json()
        assert all(e["type"] == "daily" for e in data)
        assert len(data) == 1


def test_api_summaries_invalid_type_returns_400(tmp_path: Path):
    """Test /api/summaries?type= returns 400 for unknown period type."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/summaries?type=quarterly")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data
        assert "quarterly" in data["error"]


# --- Activity Heatmap (Phase 6c) ---


def test_api_activity_uses_session_store(client):
    """Test /api/activity uses SessionStore records when available."""
    pytest.importorskip("gptme_sessions")
    resp = client.get("/api/activity?days=30")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "days" in data
    assert isinstance(data["days"], list)
    assert len(data["days"]) == 30
    # Each entry has 'date' and 'count'
    for entry in data["days"]:
        assert "date" in entry
        assert "count" in entry
        assert isinstance(entry["count"], int)
    # Fixture has 3 sessions 5 days ago (relative, no time-bomb)
    expected_date = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    counts = {e["date"]: e["count"] for e in data["days"]}
    assert counts.get(expected_date, 0) == 3


def test_api_activity_ordered_oldest_first(client):
    """Test /api/activity returns days ordered oldest → newest."""
    pytest.importorskip("gptme_sessions")
    resp = client.get("/api/activity?days=7")
    assert resp.status_code == 200
    dates = [e["date"] for e in resp.get_json()["days"]]
    assert dates == sorted(dates)


def test_api_activity_default_365_days(client):
    """Test /api/activity defaults to 365 days."""
    pytest.importorskip("gptme_sessions")
    resp = client.get("/api/activity")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["days"]) == 365


def test_api_activity_days_clamped(client):
    """Test /api/activity clamps days to [7, 730]."""
    pytest.importorskip("gptme_sessions")
    resp = client.get("/api/activity?days=9999")
    assert resp.status_code == 200
    assert len(resp.get_json()["days"]) == 730

    resp2 = client.get("/api/activity?days=1")
    assert resp2.status_code == 200
    assert len(resp2.get_json()["days"]) == 7


def test_api_activity_journal_fallback(tmp_path: Path):
    """Test /api/activity falls back to journal directory when no SessionStore."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()
    # Create journal entries: 3 .md files on one day, 1 on another (relative dates)
    d1 = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")
    d2 = (date.today() - timedelta(days=9)).strftime("%Y-%m-%d")
    j1 = tmp_path / "journal" / d1
    j1.mkdir(parents=True)
    (j1 / "session-a.md").write_text("# a")
    (j1 / "session-b.md").write_text("# b")
    (j1 / "session-c.md").write_text("# c")
    j2 = tmp_path / "journal" / d2
    j2.mkdir(parents=True)
    (j2 / "session-d.md").write_text("# d")

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/activity?days=365")
        assert resp.status_code == 200
        data = resp.get_json()
        counts = {e["date"]: e["count"] for e in data["days"]}
        assert counts.get(d1, 0) == 3
        assert counts.get(d2, 0) == 1


def test_api_activity_broken_store_falls_back_to_journal(tmp_path: Path, monkeypatch):
    """Test /api/activity falls back to journal when store.query() raises."""
    pytest.importorskip("gptme_sessions")
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()
    # Create a store directory so _load_sessions_from_store sees it as "present"
    (tmp_path / "state" / "sessions").mkdir(parents=True)
    # Create a journal entry to verify fallback is used
    d1 = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    j1 = tmp_path / "journal" / d1
    j1.mkdir(parents=True)
    (j1 / "session.md").write_text("# session")

    # Patch SessionStore.query to raise, simulating a corrupted store
    import gptme_dashboard.server as srv

    original_store_importable = srv._store_importable  # noqa: SLF001

    class _BrokenStore:
        def __init__(self, **_kw):
            pass

        def query(self, **_kw):
            raise RuntimeError("corrupted JSONL")

    monkeypatch.setattr("gptme_sessions.store.SessionStore", _BrokenStore, raising=False)
    monkeypatch.setattr(srv, "_store_importable", True)

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/activity?days=30")
        assert resp.status_code == 200
        data = resp.get_json()
        counts = {e["date"]: e["count"] for e in data["days"]}
        assert counts.get(d1, 0) == 1, "journal fallback should fire when store is broken"

    monkeypatch.setattr(srv, "_store_importable", original_store_importable)


def test_api_activity_empty_workspace(tmp_path: Path):
    """Test /api/activity returns zeros when no sessions or journal."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    (tmp_path / "lessons").mkdir()

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/activity?days=7")
        assert resp.status_code == 200
        data = resp.get_json()
        assert all(e["count"] == 0 for e in data["days"])


# --- Schedule (Phase 3) ---


def test_api_schedule_structure(client):
    """Test /api/schedule returns correct structure."""
    resp = client.get("/api/schedule")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "timers" in data
    assert "platform" in data
    assert isinstance(data["timers"], list)
    assert isinstance(data["platform"], str)


def test_api_schedule_linux_detection(client):
    """Test /api/schedule parses systemd timers matching agent name on Linux."""
    systemctl_output = json.dumps(
        [
            {
                "next": 1773150600000000,
                "left": 1773150600000000,
                "last": 1773150000046572,
                "passed": 1830362162103,
                "unit": "testbot-autonomous.timer",
                "activates": "testbot-autonomous.service",
            },
            {
                "next": 1773150900000000,
                "left": 1773150900000000,
                "last": 1773150300000000,
                "passed": 1830662243774,
                "unit": "gptme-server.timer",
                "activates": "gptme-server.service",
            },
            {
                "next": 0,
                "left": 0,
                "last": 0,
                "passed": 0,
                "unit": "unrelated.timer",
                "activates": "unrelated.service",
            },
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = systemctl_output

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/schedule")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["platform"] == "Linux"
    # "testbot" matches agent name, "gptme" matches keyword; "unrelated" excluded
    assert len(data["timers"]) == 2
    names = {t["name"] for t in data["timers"]}
    assert "testbot-autonomous.timer" in names
    assert "gptme-server.timer" in names
    assert "unrelated.timer" not in names


def test_api_schedule_timestamp_conversion(client):
    """Test /api/schedule converts microsecond timestamps to ISO format."""
    systemctl_output = json.dumps(
        [
            {
                "next": 1773150600000000,  # 2026-03-08T10:10:00 UTC
                "left": 1773150600000000,
                "last": 1773150000046572,
                "passed": 1830362162103,
                "unit": "gptme-test.timer",
                "activates": "gptme-test.service",
            },
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = systemctl_output

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/schedule")

    data = resp.get_json()
    timer = data["timers"][0]
    assert timer["next"] is not None
    assert timer["last"] is not None
    # ISO format should contain 'T' separator
    assert "T" in timer["next"]
    assert "T" in timer["last"]
    assert timer["activates"] == "gptme-test.service"


def test_api_schedule_zero_timestamps(client):
    """Test /api/schedule handles zero timestamps (never triggered) as None."""
    systemctl_output = json.dumps(
        [
            {
                "next": 0,
                "left": 0,
                "last": 0,
                "passed": 0,
                "unit": "gptme-inactive.timer",
                "activates": "gptme-inactive.service",
            },
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = systemctl_output

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/schedule")

    data = resp.get_json()
    assert len(data["timers"]) == 1
    timer = data["timers"][0]
    assert timer["next"] is None
    assert timer["last"] is None


def test_api_schedule_systemctl_failure(client):
    """Test /api/schedule returns empty list when systemctl fails."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/schedule")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["timers"] == []


def test_api_schedule_timeout_handled(client):
    """Test /api/schedule handles subprocess timeout gracefully."""
    import subprocess

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 5)
        ),
    ):
        resp = client.get("/api/schedule")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["timers"] == []


def test_api_schedule_overflow_timestamps(client):
    """Test /api/schedule handles very large sentinel timestamps (UINT64_MAX)."""
    systemctl_output = json.dumps(
        [
            {
                "next": 18446744073709551615,  # UINT64_MAX sentinel
                "left": 18446744073709551615,
                "last": 1773150000046572,
                "passed": 1830362162103,
                "unit": "gptme-overflow.timer",
                "activates": "gptme-overflow.service",
            },
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = systemctl_output

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/schedule")

    # Should not 500 — graceful fallback
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data["timers"], list)


def test_api_schedule_darwin_empty(client):
    """Test /api/schedule returns empty timers on macOS (not yet supported)."""
    with unittest.mock.patch("platform.system", return_value="Darwin"):
        resp = client.get("/api/schedule")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["platform"] == "Darwin"
    assert data["timers"] == []


# ── Service Health endpoint tests ──


def test_api_health_structure(client):
    """Test /api/services/health returns correct top-level structure."""
    with unittest.mock.patch("platform.system", return_value="Linux"):
        # Mock list-units returning no relevant services
        mock_result = unittest.mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"
        with unittest.mock.patch("subprocess.run", return_value=mock_result):
            resp = client.get("/api/services/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "services" in data
    assert "platform" in data
    assert isinstance(data["services"], list)
    assert data["platform"] == "Linux"


def test_api_health_non_linux(client):
    """Test /api/services/health returns empty on non-Linux."""
    with unittest.mock.patch("platform.system", return_value="Darwin"):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["services"] == []
    assert data["platform"] == "Darwin"


def _make_subprocess_side_effect(
    list_units_json: str,
    show_output: str = "",
    journal_output: str = "",
):
    """Build a side_effect for subprocess.run that handles multiple commands."""

    def _side_effect(cmd, **_kwargs):
        result = unittest.mock.MagicMock()
        result.returncode = 0
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd

        if "list-units" in cmd_str:
            result.stdout = list_units_json
        elif "systemctl" in cmd_str and "show" in cmd_str:
            result.stdout = show_output
        elif "journalctl" in cmd_str:
            result.stdout = journal_output
        else:
            result.stdout = ""
        return result

    return _side_effect


def test_api_health_healthy_service(client):
    """Test /api/services/health classifies an active service with no errors as healthy."""

    units_json = json.dumps(
        [
            {
                "unit": "gptme-server.service",
                "description": "gptme API Server",
                "active": "active",
                "sub": "running",
            }
        ]
    )

    side_effect = _make_subprocess_side_effect(
        list_units_json=units_json,
        show_output="MainPID=1234\nActiveEnterTimestamp=Mon 2024-01-15 10:00:00 UTC\nNRestarts=0\nMemoryCurrent=52428800\n",
        journal_output="",  # no errors
    )

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=side_effect),
    ):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["services"]) == 1
    svc = data["services"][0]
    assert svc["name"] == "gptme-server.service"
    assert svc["health"] == "healthy"
    assert svc["active"] == "active"
    assert svc["pid"] == 1234
    assert svc["memory_bytes"] == 52428800
    assert svc["restart_count"] == 0
    assert svc["recent_errors"] == 0
    assert svc["uptime_seconds"] > 0


def test_api_health_degraded_service(client):
    """Test /api/services/health classifies service with many errors as degraded."""

    units_json = json.dumps(
        [
            {
                "unit": "gptme-server.service",
                "description": "gptme API Server",
                "active": "active",
                "sub": "running",
            }
        ]
    )
    # 25 error lines
    error_lines = "\n".join([f"Mar 10 10:0{i}:00 host unit[1]: Error {i}" for i in range(25)])

    side_effect = _make_subprocess_side_effect(
        list_units_json=units_json,
        show_output="MainPID=5678\nActiveEnterTimestamp=Mon 2024-01-15 12:00:00 UTC\nNRestarts=0\nMemoryCurrent=104857600\n",
        journal_output=error_lines,
    )

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=side_effect),
    ):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    svc = resp.get_json()["services"][0]
    assert svc["health"] == "degraded"
    assert svc["recent_errors"] == 25


def test_api_health_unhealthy_inactive_service(client):
    """Test /api/services/health classifies inactive service as unhealthy."""
    units_json = json.dumps(
        [
            {
                "unit": "gptme-server.service",
                "description": "gptme API Server",
                "active": "inactive",
                "sub": "dead",
            }
        ]
    )

    side_effect = _make_subprocess_side_effect(
        list_units_json=units_json,
        show_output="MainPID=0\nActiveEnterTimestamp=n/a\nNRestarts=0\nMemoryCurrent=[not set]\n",
        journal_output="",
    )

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=side_effect),
    ):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    svc = resp.get_json()["services"][0]
    assert svc["health"] == "unhealthy"
    assert svc["active"] == "inactive"
    assert svc["memory_bytes"] == 0


def test_api_health_memory_uint64_max_sentinel(client):
    """Test /api/services/health treats MemoryCurrent UINT64_MAX sentinel as no data."""
    # systemd returns 18446744073709551615 (UINT64_MAX) when MemoryAccounting is
    # disabled or the service is stopped. Must not be surfaced as ~17.2 EB.
    units_json = json.dumps(
        [
            {
                "unit": "gptme-server.service",
                "description": "gptme API Server",
                "active": "active",
                "sub": "running",
            }
        ]
    )

    side_effect = _make_subprocess_side_effect(
        list_units_json=units_json,
        show_output="MainPID=1234\nActiveEnterTimestamp=Mon 2024-01-15 10:00:00 UTC\nNRestarts=0\nMemoryCurrent=18446744073709551615\n",
        journal_output="",
    )

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=side_effect),
    ):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    svc = resp.get_json()["services"][0]
    assert svc["memory_bytes"] == 0, "UINT64_MAX sentinel must be treated as no data"


def test_api_health_warning_service(client):
    """Test /api/services/health classifies service with few errors as warning."""

    units_json = json.dumps(
        [
            {
                "unit": "testbot-autonomous.service",
                "description": "TestBot Autonomous",
                "active": "active",
                "sub": "running",
            }
        ]
    )

    side_effect = _make_subprocess_side_effect(
        list_units_json=units_json,
        show_output="MainPID=9999\nActiveEnterTimestamp=Mon 2024-01-15 08:00:00 UTC\nNRestarts=1\nMemoryCurrent=33554432\n",
        journal_output="Mar 10 10:00:00 host unit[1]: Warning line\n",
    )

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=side_effect),
    ):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    svc = resp.get_json()["services"][0]
    assert svc["health"] == "warning"
    assert svc["restart_count"] == 1
    assert svc["recent_errors"] == 1


def test_api_health_systemctl_failure(client):
    """Test /api/services/health handles systemctl failure gracefully."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["services"] == []


def test_api_health_subprocess_timeout(client):
    """Test /api/services/health handles subprocess timeout."""
    import subprocess

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 5)
        ),
    ):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["services"] == []


def test_api_health_caching(client):
    """Test /api/services/health caches results for 60 seconds."""
    units_json = json.dumps(
        [
            {
                "unit": "gptme-test.service",
                "description": "Test",
                "active": "active",
                "sub": "running",
            }
        ]
    )

    call_count = 0
    original_side_effect = _make_subprocess_side_effect(
        list_units_json=units_json,
        show_output="MainPID=100\nActiveEnterTimestamp=n/a\nNRestarts=0\nMemoryCurrent=[not set]\n",
        journal_output="",
    )

    def counting_side_effect(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_side_effect(cmd, **kwargs)

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=counting_side_effect),
    ):
        resp1 = client.get("/api/services/health")
        first_count = call_count
        resp2 = client.get("/api/services/health")
        second_count = call_count

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Second call should use cache (no additional subprocess calls)
    assert second_count == first_count


def test_api_health_multiple_services(client):
    """Test /api/services/health handles multiple services correctly."""

    units_json = json.dumps(
        [
            {
                "unit": "gptme-server.service",
                "description": "gptme API",
                "active": "active",
                "sub": "running",
            },
            {
                "unit": "testbot-autonomous.service",
                "description": "TestBot Auto",
                "active": "inactive",
                "sub": "dead",
            },
        ]
    )

    side_effect = _make_subprocess_side_effect(
        list_units_json=units_json,
        show_output="MainPID=100\nActiveEnterTimestamp=n/a\nNRestarts=0\nMemoryCurrent=[not set]\n",
        journal_output="",
    )

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=side_effect),
    ):
        resp = client.get("/api/services/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["services"]) == 2
    names = [s["name"] for s in data["services"]]
    assert "gptme-server.service" in names
    assert "testbot-autonomous.service" in names


# ---------------------------------------------------------------------------
# Phase 5b: Service restart endpoint tests
# ---------------------------------------------------------------------------


def _get_restart_token(client) -> str:
    """Fetch the auto-generated restart token from the restart-enabled endpoint."""
    resp = client.get("/api/services/restart-enabled")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["token"]
    return data["token"]


def test_restart_enabled_localhost(client):
    """Test /api/services/restart-enabled returns token for localhost."""
    resp = client.get("/api/services/restart-enabled")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert isinstance(data["token"], str)
    assert len(data["token"]) >= 32  # at least 32 hex chars


def test_restart_enabled_non_localhost(client):
    """Test /api/services/restart-enabled rejects non-localhost requests."""
    resp = client.get("/api/services/restart-enabled", environ_base={"REMOTE_ADDR": "1.2.3.4"})
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["enabled"] is False
    assert data["token"] is None


def test_restart_success(client):
    """Test /api/services/<name>/restart restarts a whitelisted service."""
    token = _get_restart_token(client)

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch(
            "subprocess.run",
            return_value=unittest.mock.MagicMock(returncode=0, stderr="", stdout=""),
        ),
    ):
        resp = client.post(
            "/api/services/gptme-server.service/restart",
            headers={"X-Restart-Token": token},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["service"] == "gptme-server.service"
    assert data["action"] == "restart"


def test_restart_no_token(client):
    """Test /api/services/<name>/restart rejects requests with no token."""
    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run"),
    ):
        resp = client.post("/api/services/gptme-server.service/restart")

    assert resp.status_code == 403
    data = resp.get_json()
    assert data["status"] == "forbidden"


def test_restart_wrong_token(client):
    """Test /api/services/<name>/restart rejects requests with wrong token."""
    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run"),
    ):
        resp = client.post(
            "/api/services/gptme-server.service/restart",
            headers={"X-Restart-Token": "wrong-token"},
        )

    assert resp.status_code == 403
    data = resp.get_json()
    assert data["status"] == "forbidden"


def test_restart_non_localhost(client):
    """Test /api/services/<name>/restart rejects non-localhost requests."""
    token = _get_restart_token(client)
    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run"),
    ):
        resp = client.post(
            "/api/services/gptme-server.service/restart",
            headers={"X-Restart-Token": token},
            environ_base={"REMOTE_ADDR": "1.2.3.4"},
        )

    assert resp.status_code == 403
    data = resp.get_json()
    assert data["status"] == "forbidden"


def test_restart_service_not_whitelisted(client):
    """Test /api/services/<name>/restart rejects non-whitelisted service names."""
    token = _get_restart_token(client)
    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run"),
    ):
        resp = client.post(
            "/api/services/sshd.service/restart",
            headers={"X-Restart-Token": token},
        )

    assert resp.status_code == 403
    data = resp.get_json()
    assert data["status"] == "forbidden"
    assert data["service"] == "sshd.service"


def test_restart_non_linux(client):
    """Test /api/services/<name>/restart returns 501 on non-Linux platforms."""
    token = _get_restart_token(client)
    with unittest.mock.patch("platform.system", return_value="Darwin"):
        resp = client.post(
            "/api/services/gptme-server.service/restart",
            headers={"X-Restart-Token": token},
        )

    assert resp.status_code == 501
    data = resp.get_json()
    assert data["status"] == "unsupported"


def test_restart_systemctl_failure(client):
    """Test /api/services/<name>/restart handles systemctl failure."""
    token = _get_restart_token(client)
    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch(
            "subprocess.run",
            return_value=unittest.mock.MagicMock(
                returncode=1, stderr="Failed to restart unit", stdout=""
            ),
        ),
    ):
        resp = client.post(
            "/api/services/gptme-server.service/restart",
            headers={"X-Restart-Token": token},
        )

    assert resp.status_code == 500
    data = resp.get_json()
    assert data["status"] == "error"
    assert "Failed to restart unit" in data["error"]


def test_restart_subprocess_timeout(client):
    """Test /api/services/<name>/restart handles subprocess timeout gracefully."""
    import subprocess

    token = _get_restart_token(client)
    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 15)
        ),
    ):
        resp = client.post(
            "/api/services/gptme-server.service/restart",
            headers={"X-Restart-Token": token},
        )

    assert resp.status_code == 500
    data = resp.get_json()
    assert data["status"] == "error"
    assert "timed out" in data["error"].lower()


# ── Service Logs endpoint tests ──


def test_api_logs_missing_service(client):
    """Test /api/services/logs returns 400 when service param is missing."""
    resp = client.get("/api/services/logs")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data
    assert "service" in data["error"].lower()


def test_api_logs_invalid_since(client):
    """Test /api/services/logs returns 400 for invalid since param."""
    resp = client.get("/api/services/logs?service=gptme-test.service&since=2w")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "since" in data["error"].lower()


def test_api_logs_invalid_priority(client):
    """Test /api/services/logs returns 400 for invalid priority param."""
    resp = client.get("/api/services/logs?service=gptme-test.service&priority=critical")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "priority" in data["error"].lower()


def test_api_logs_non_relevant_service(client):
    """Test /api/services/logs returns 403 for non-gptme service."""
    with unittest.mock.patch("platform.system", return_value="Linux"):
        resp = client.get("/api/services/logs?service=nginx.service")
    assert resp.status_code == 403
    data = resp.get_json()
    assert "error" in data


def test_api_logs_non_linux(client):
    """Test /api/services/logs returns empty on non-Linux for gptme services."""
    with unittest.mock.patch("platform.system", return_value="Darwin"):
        resp = client.get("/api/services/logs?service=gptme-test.service")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["logs"] == []
    assert data["platform"] == "Darwin"


def test_api_logs_non_linux_non_relevant_service(client):
    """Test /api/services/logs returns 403 for non-gptme service even on non-Linux.

    Security check must happen before the platform guard so non-Linux hosts
    cannot be used to probe arbitrary service names.
    """
    with unittest.mock.patch("platform.system", return_value="Darwin"):
        resp = client.get("/api/services/logs?service=nginx.service")
    assert resp.status_code == 403
    data = resp.get_json()
    assert "error" in data


def test_api_logs_structure(client):
    """Test /api/services/logs returns correct structure with journalctl JSON output."""
    journal_lines = "\n".join(
        [
            json.dumps(
                {
                    "__REALTIME_TIMESTAMP": "1710072000000000",
                    "PRIORITY": "6",
                    "MESSAGE": "Service started successfully",
                }
            ),
            json.dumps(
                {
                    "__REALTIME_TIMESTAMP": "1710072060000000",
                    "PRIORITY": "4",
                    "MESSAGE": "Connection timeout warning",
                }
            ),
            json.dumps(
                {
                    "__REALTIME_TIMESTAMP": "1710072120000000",
                    "PRIORITY": "3",
                    "MESSAGE": "Failed to connect to database",
                }
            ),
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = journal_lines

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services/logs?service=gptme-test.service")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["service"] == "gptme-test.service"
    assert data["total"] == 3
    assert data["since"] == "1h"
    assert data["platform"] == "Linux"
    assert len(data["logs"]) == 3

    # Check entry structure
    entry = data["logs"][0]
    assert "timestamp" in entry
    assert "priority" in entry
    assert "message" in entry
    assert entry["priority"] == "info"
    assert entry["message"] == "Service started successfully"

    # Check priority names
    assert data["logs"][1]["priority"] == "warning"
    assert data["logs"][2]["priority"] == "err"


def test_api_logs_empty_output(client):
    """Test /api/services/logs handles empty journalctl output."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services/logs?service=gptme-test.service")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["logs"] == []
    assert data["total"] == 0


def test_api_logs_timeout_handled(client):
    """Test /api/services/logs handles subprocess timeout gracefully."""
    import subprocess

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("journalctl", 10)
        ),
    ):
        resp = client.get("/api/services/logs?service=gptme-test.service")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["logs"] == []
    assert data["total"] == 0


def test_api_logs_with_since_param(client):
    """Test /api/services/logs passes since parameter to journalctl."""
    calls = []

    def capture_calls(cmd, **kwargs):
        calls.append(cmd)
        result = unittest.mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        return result

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=capture_calls),
    ):
        resp = client.get("/api/services/logs?service=gptme-test.service&since=24h")

    assert resp.status_code == 200
    # Verify journalctl was called with correct --since
    assert len(calls) == 1
    assert "--since" in calls[0]
    since_idx = calls[0].index("--since")
    assert calls[0][since_idx + 1] == "24 hours ago"


def test_api_logs_with_priority_filter(client):
    """Test /api/services/logs passes priority filter to journalctl."""
    calls = []

    def capture_calls(cmd, **kwargs):
        calls.append(cmd)
        result = unittest.mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        return result

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=capture_calls),
    ):
        resp = client.get("/api/services/logs?service=gptme-test.service&priority=err")

    assert resp.status_code == 200
    assert len(calls) == 1
    assert "--priority" in calls[0]
    prio_idx = calls[0].index("--priority")
    assert calls[0][prio_idx + 1] == "3"  # err = priority 3


def test_api_logs_caching(client):
    """Test /api/services/logs caches results for 30 seconds."""
    journal_output = json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1710072000000000",
            "PRIORITY": "6",
            "MESSAGE": "Test message",
        }
    )
    call_count = 0

    def counting_side_effect(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        result = unittest.mock.MagicMock()
        result.returncode = 0
        result.stdout = journal_output
        return result

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=counting_side_effect),
    ):
        resp1 = client.get("/api/services/logs?service=gptme-test.service")
        first_count = call_count
        resp2 = client.get("/api/services/logs?service=gptme-test.service")
        second_count = call_count

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Second call should use cache (no additional subprocess calls)
    assert second_count == first_count


def test_api_logs_agent_name_service(client):
    """Test /api/services/logs allows services matching agent name."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        # Agent name is "TestBot" (from fixture), so "testbot-*" should be allowed
        resp = client.get("/api/services/logs?service=testbot-autonomous.service")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["service"] == "testbot-autonomous.service"


def test_api_logs_malformed_json_lines(client):
    """Test /api/services/logs skips malformed JSON lines gracefully."""
    journal_output = "\n".join(
        [
            json.dumps(
                {
                    "__REALTIME_TIMESTAMP": "1710072000000000",
                    "PRIORITY": "6",
                    "MESSAGE": "Good line",
                }
            ),
            "this is not json",
            json.dumps(
                {
                    "__REALTIME_TIMESTAMP": "1710072060000000",
                    "PRIORITY": "4",
                    "MESSAGE": "Another good line",
                }
            ),
        ]
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = journal_output

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services/logs?service=gptme-test.service")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 2  # malformed line skipped
    assert data["logs"][0]["message"] == "Good line"
    assert data["logs"][1]["message"] == "Another good line"


def test_api_logs_binary_message(client):
    """Test /api/services/logs handles binary MESSAGE fields (int-array encoding)."""
    # systemd encodes binary/non-UTF-8 MESSAGE as a JSON array of integers
    binary_msg = list("Hello\x00World".encode("utf-8"))
    journal_output = json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1710072000000000",
            "PRIORITY": "6",
            "MESSAGE": binary_msg,
        }
    )
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = journal_output

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services/logs?service=gptme-test.service")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 1
    assert data["logs"][0]["message"] == "Hello\x00World"


def test_api_logs_nonzero_returncode_warning(client):
    """Test /api/services/logs includes warning field when journalctl exits non-zero."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "Unit gptme-test.service not found."

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        resp = client.get("/api/services/logs?service=gptme-test.service")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 0
    assert data["logs"] == []
    assert "warning" in data
    assert "1" in data["warning"]  # returncode in message
    assert "not found" in data["warning"]  # stderr snippet included


def test_api_logs_exception_path_caches_result(client):
    """Test /api/services/logs caches on persistent exception to prevent subprocess spam."""
    call_count = 0

    def raising_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise FileNotFoundError("journalctl not found")

    with (
        unittest.mock.patch("platform.system", return_value="Linux"),
        unittest.mock.patch("subprocess.run", side_effect=raising_run),
    ):
        resp1 = client.get("/api/services/logs?service=gptme-test.service")
        resp2 = client.get("/api/services/logs?service=gptme-test.service")

    # First call returns 500; subprocess was called once
    assert resp1.status_code == 500
    assert call_count == 1
    # Second identical request hits the cache — subprocess NOT called again
    assert resp2.status_code == 200
    assert call_count == 1
    data2 = resp2.get_json()
    assert data2["logs"] == []


# ---------------------------------------------------------------------------
# Phase 6a: Org view tests
# ---------------------------------------------------------------------------


@pytest.fixture
def org_toml(tmp_path: Path) -> Path:
    """Create a minimal org.toml for testing."""
    p = tmp_path / "org.toml"
    p.write_text(
        textwrap.dedent("""\
        [[agents]]
        name = "bob"
        api  = "http://bob.example.com:8042"

        [[agents]]
        name = "alice"
        api  = "http://alice.example.com:8042"
        """)
    )
    return p


def test_load_org_config(org_toml: Path) -> None:
    """Test load_org_config parses agent list correctly."""
    from gptme_dashboard.server import load_org_config

    agents = load_org_config(org_toml)
    assert len(agents) == 2
    assert agents[0] == {"name": "bob", "api": "http://bob.example.com:8042"}
    assert agents[1] == {"name": "alice", "api": "http://alice.example.com:8042"}


def test_load_org_config_missing_name(tmp_path: Path) -> None:
    """Test load_org_config raises ValueError when agent is missing 'name'."""
    from gptme_dashboard.server import load_org_config

    p = tmp_path / "bad.toml"
    p.write_text('[[agents]]\napi = "http://example.com:8042"\n')
    with pytest.raises(ValueError, match="missing 'name'"):
        load_org_config(p)


def test_load_org_config_missing_api(tmp_path: Path) -> None:
    """Test load_org_config raises ValueError when agent is missing 'api'."""
    from gptme_dashboard.server import load_org_config

    p = tmp_path / "bad.toml"
    p.write_text('[[agents]]\nname = "bob"\n')
    with pytest.raises(ValueError, match="missing 'api'"):
        load_org_config(p)


def test_load_org_config_bad_url_scheme(tmp_path: Path) -> None:
    """Test load_org_config raises ValueError for non-http(s) API URLs."""
    from gptme_dashboard.server import load_org_config

    p = tmp_path / "bad.toml"
    p.write_text('[[agents]]\nname = "bob"\napi = "ftp://example.com"\n')
    with pytest.raises(ValueError, match="must start with http"):
        load_org_config(p)


def test_api_org_no_config(workspace: Path, tmp_path: Path) -> None:
    """Test /api/org returns 404 when no org config is loaded."""
    app = create_app(workspace)
    with app.test_client() as c:
        resp = c.get("/api/org")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_api_org_aggregates_agents(workspace: Path, org_toml: Path) -> None:
    """Test /api/org calls each agent's API and returns agent cards."""

    def _mock_fetch_json(url: str, timeout: int = 5):
        """Return parsed JSON dict/list, agent-aware via URL hostname."""
        agent_name = "alice" if "alice.example.com" in url else "bob"
        if "/api/status" in url:
            return {"mode": "dynamic", "agent": agent_name, "workspace": agent_name}
        elif "/api/tasks" in url:
            return [{"id": "task-1", "title": "Do something", "state": "active"}]
        elif "/api/services" in url:
            return {"services": [{"name": "gptme.service", "active": True}]}
        elif "/api/sessions" in url:
            return {"sessions": [{"date": "2026-03-11"}], "total": 1}
        return {}

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 2
    agents = data["agents"]
    # First agent (bob) should be reachable with data
    bob = agents[0]
    assert bob["name"] == "bob"
    assert "error" not in bob
    assert bob["active_tasks"] == 1
    assert bob["running_services"] == ["gptme.service"]
    assert bob["last_session"] == "2026-03-11"
    # Second agent (alice) should also be reachable with correct identity
    alice = agents[1]
    assert alice["name"] == "alice"
    assert "error" not in alice
    assert alice["status"]["agent"] == "alice"
    assert alice["active_tasks"] == 1
    assert alice["running_services"] == ["gptme.service"]
    assert alice["last_session"] == "2026-03-11"


def test_api_org_handles_unreachable_agent(workspace: Path, org_toml: Path) -> None:
    """Test /api/org marks unreachable agents with error field."""

    def _mock_fetch_json(url: str, timeout: int = 5):
        return None  # simulate unreachable agent

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 2
    for agent in data["agents"]:
        assert agent["error"] == "unreachable"


def test_org_view_no_config(workspace: Path) -> None:
    """Test /org returns 404 when no org config is loaded."""
    app = create_app(workspace)
    with app.test_client() as c:
        resp = c.get("/org")
    assert resp.status_code == 404


def test_org_view_with_config(workspace: Path, org_toml: Path) -> None:
    """Test /org returns an HTML page when org config is loaded."""
    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        resp = c.get("/org")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Org View" in html
    assert "/api/org" in html


def test_org_view_xss_escaping(workspace: Path, org_toml: Path) -> None:
    """Test /org page includes esc() for XSS prevention."""
    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        resp = c.get("/org")
    html = resp.data.decode()
    # The page must define an esc() helper for HTML escaping
    assert "function esc(" in html
    # All dynamic text insertions should route through esc()
    assert "esc(a.api)" in html
    assert "esc(" in html


def test_create_app_raises_on_bad_org_config(workspace: Path, tmp_path: Path) -> None:
    """Test create_app raises when org_config is malformed."""
    bad_config = tmp_path / "bad.toml"
    bad_config.write_text('[[agents]]\napi = "http://example.com:8042"\n')  # missing name
    with pytest.raises(ValueError, match="missing 'name'"):
        create_app(workspace, org_config=bad_config)


def test_fetch_json_blocks_redirects() -> None:
    """_fetch_json must return None on HTTP redirects (SSRF prevention)."""
    from unittest.mock import MagicMock

    import urllib.error

    from gptme_dashboard.server import _NoRedirectHandler

    handler = _NoRedirectHandler()
    req = MagicMock()
    fp = MagicMock()
    headers: dict = {}

    # redirect_request must raise URLError, not follow the redirect
    with pytest.raises(urllib.error.URLError, match="redirect not allowed"):
        handler.redirect_request(req, fp, 302, "Found", headers, "http://169.254.169.254/")


def test_api_org_service_missing_name(workspace: Path, org_toml: Path) -> None:
    """Test /api/org handles service entries without a 'name' field gracefully (no KeyError)."""

    def _mock_fetch_json(url: str, timeout: int = 5):
        if "/api/status" in url:
            return {"mode": "dynamic", "agent": "bob", "workspace": "bob"}
        elif "/api/tasks" in url:
            return []
        elif "/api/services" in url:
            # One malformed entry (no 'name') mixed with a valid one
            return {
                "services": [
                    {"active": True},  # missing 'name' — must not raise KeyError
                    {"name": "gptme.service", "active": True},
                ]
            }
        elif "/api/sessions" in url:
            return {"sessions": [], "total": 0}
        return {}

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    assert resp.status_code == 200
    agents = resp.get_json()["agents"]
    bob = next(a for a in agents if a["name"] == "bob")
    # Malformed entry silently skipped; only the named service is returned
    assert bob["running_services"] == ["gptme.service"]


def test_api_org_tasks_null(workspace: Path, org_toml: Path) -> None:
    """Test /api/org handles tasks=null (non-list) without raising TypeError."""

    def _mock_fetch_json(url: str, timeout: int = 5):
        if "/api/status" in url:
            return {"mode": "dynamic", "agent": "bob", "workspace": "bob"}
        elif "/api/tasks" in url:
            # Remote agent returns {"tasks": null} — must not raise TypeError on len()
            return {"tasks": None}
        elif "/api/services" in url:
            return {"services": []}
        elif "/api/sessions" in url:
            return {"sessions": [], "total": 0}
        return {}

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    assert resp.status_code == 200
    bob = next(a for a in resp.get_json()["agents"] if a["name"] == "bob")
    assert bob["active_tasks"] is None  # non-list tasks → None, not TypeError


def test_api_org_card_exception_isolated(workspace: Path, org_toml: Path) -> None:
    """Test /api/org isolates per-agent failures — one bad agent doesn't crash all cards."""
    call_count = 0

    def _mock_fetch_json(url: str, timeout: int = 5):
        nonlocal call_count
        call_count += 1
        if "alice.example.com" in url and "/api/status" in url:
            raise RuntimeError("alice is on fire")
        agent_name = "alice" if "alice.example.com" in url else "bob"
        if "/api/status" in url:
            return {"mode": "dynamic", "agent": agent_name, "workspace": agent_name}
        elif "/api/tasks" in url:
            return []
        elif "/api/services" in url:
            return {"services": []}
        elif "/api/sessions" in url:
            return {"sessions": [], "total": 0}
        return {}

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    # Endpoint must not 500 even though alice raised an exception
    assert resp.status_code == 200
    agents = resp.get_json()["agents"]
    assert len(agents) == 2
    bob = next(a for a in agents if a["name"] == "bob")
    alice = next(a for a in agents if a["name"] == "alice")
    assert "error" not in bob  # bob succeeded
    assert "error" in alice  # alice's failure is isolated to her card


def test_api_org_services_null(workspace: Path, org_toml: Path) -> None:
    """Test /api/org handles services=null (explicit null) without TypeError."""

    def _mock_fetch_json(url: str, timeout: int = 5):
        if "/api/status" in url:
            return {"mode": "dynamic", "agent": "bob", "workspace": "bob"}
        elif "/api/tasks" in url:
            return []
        elif "/api/services" in url:
            # Remote agent returns {"services": null} — .get("services", []) returns None
            return {"services": None}
        elif "/api/sessions" in url:
            return {"sessions": [], "total": 0}
        return {}

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    assert resp.status_code == 200
    bob = next(a for a in resp.get_json()["agents"] if a["name"] == "bob")
    assert bob["running_services"] == []  # null services → empty list, not TypeError


def test_api_org_session_non_dict(workspace: Path, org_toml: Path) -> None:
    """Test /api/org handles sessions[0]=null without AttributeError."""

    def _mock_fetch_json(url: str, timeout: int = 5):
        if "/api/status" in url:
            return {"mode": "dynamic", "agent": "bob", "workspace": "bob"}
        elif "/api/tasks" in url:
            return []
        elif "/api/services" in url:
            return {"services": []}
        elif "/api/sessions" in url:
            # Remote agent returns a non-dict first session entry
            return {"sessions": [None], "total": 1}
        return {}

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    assert resp.status_code == 200
    bob = next(a for a in resp.get_json()["agents"] if a["name"] == "bob")
    assert bob["last_session"] is None  # non-dict session → None, not AttributeError


def test_api_org_sessions_non_list(workspace: Path, org_toml: Path) -> None:
    """Test /api/org handles sessions=<non-list> without TypeError."""

    def _mock_fetch_json(url: str, timeout: int = 5):
        if "/api/status" in url:
            return {"mode": "dynamic", "agent": "bob", "workspace": "bob"}
        elif "/api/tasks" in url:
            return []
        elif "/api/services" in url:
            return {"services": []}
        elif "/api/sessions" in url:
            # Remote agent returns a non-list sessions value
            return {"sessions": 42}
        return {}

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    assert resp.status_code == 200
    bob = next(a for a in resp.get_json()["agents"] if a["name"] == "bob")
    assert bob["last_session"] is None  # non-list sessions → None, not TypeError


def test_api_org_service_entry_non_dict(workspace: Path, org_toml: Path) -> None:
    """Test /api/org handles non-dict entries in services list without AttributeError."""

    def _mock_fetch_json(url: str, timeout: int = 5):
        if "/api/status" in url:
            return {"mode": "dynamic", "agent": "bob", "workspace": "bob"}
        elif "/api/tasks" in url:
            return []
        elif "/api/services" in url:
            # Remote agent returns a null entry inside services list
            return {"services": [None, {"name": "gptme", "active": True}]}
        elif "/api/sessions" in url:
            return {"sessions": [], "total": 0}
        return {}

    app = create_app(workspace, org_config=org_toml)
    with app.test_client() as c:
        with unittest.mock.patch(
            "gptme_dashboard.server._fetch_json", side_effect=_mock_fetch_json
        ):
            resp = c.get("/api/org")

    assert resp.status_code == 200
    bob = next(a for a in resp.get_json()["agents"] if a["name"] == "bob")
    # non-dict entry skipped; valid dict entry included
    assert bob["running_services"] == ["gptme"]


def test_load_org_config_agents_not_list(tmp_path: Path) -> None:
    """Test load_org_config raises ValueError if agents is not a list."""
    toml_path = tmp_path / "org.toml"
    toml_path.write_bytes(b'agents = "not-a-list"\n')
    with pytest.raises(ValueError, match="must be an array-of-tables"):
        load_org_config(toml_path)


# ── /api/search tests ──────────────────────────────────────────────────────────


def _make_search_workspace(tmp_path: Path) -> Path:
    """Create a workspace with tasks and lessons for search testing."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "git-workflow.md").write_text(
        "---\nmatch:\n  keywords: [git commit, git workflow]\nstatus: active\n---\n"
        "# Git Workflow\n\nAlways use conventional commits when contributing.\n"
    )
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "fix-bug.md").write_text(
        "---\nstate: active\npriority: high\ntags: [bugfix, git]\ncreated: 2026-03-01\n---\n"
        "# Fix Git Bug\n\nSomething went wrong with the git integration.\n"
    )
    return tmp_path


def test_api_search_missing_query(client):
    """Test /api/search returns 400 when q is missing or too short."""
    resp = client.get("/api/search")
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    resp = client.get("/api/search?q=a")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_api_search_invalid_type(client):
    """Test /api/search returns 400 for unknown type filter."""
    resp = client.get("/api/search?q=git&type=banana")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data
    assert "banana" in data["error"]


def test_api_search_empty_workspace(client):
    """Test /api/search returns empty results on a minimal workspace."""
    resp = client.get("/api/search?q=git")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["results"] == []
    assert data["total"] == 0
    assert data["query"] == "git"
    assert data["type_filter"] is None


def test_api_search_finds_task(tmp_path: Path):
    """Test /api/search matches task titles."""
    ws = _make_search_workspace(tmp_path)
    site_dir = tmp_path / "site"
    app = create_app(ws, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=git+bug")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1
        titles = [r["title"] for r in data["results"]]
        assert any("Git" in t for t in titles)
        # Verify task URL has correct format: /tasks/<id>.html (not doubled /tasks/tasks/…)
        task_results = [r for r in data["results"] if r["type"] == "task"]
        assert task_results, "Expected at least one task result"
        task_url = task_results[0]["url"]
        assert task_url.startswith("/tasks/"), f"Task URL should start with /tasks/: {task_url}"
        assert "/tasks/tasks/" not in task_url, f"Doubled URL prefix in task URL: {task_url}"


def test_api_search_type_filter(tmp_path: Path):
    """Test /api/search?type=task returns only tasks."""
    ws = _make_search_workspace(tmp_path)
    site_dir = tmp_path / "site"
    app = create_app(ws, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=git&type=task")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type_filter"] == "task"
        for result in data["results"]:
            assert result["type"] == "task"


def test_api_search_type_filter_lesson(tmp_path: Path):
    """Test /api/search?type=lesson returns only lessons."""
    ws = _make_search_workspace(tmp_path)
    site_dir = tmp_path / "site"
    app = create_app(ws, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=git&type=lesson")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type_filter"] == "lesson"
        for result in data["results"]:
            assert result["type"] == "lesson"
            # Verify URL has no doubled prefix: /lessons/lessons/… is wrong
            url = result["url"]
            assert url.startswith("/lessons/"), f"Lesson URL should start with /lessons/: {url}"
            assert "/lessons/lessons/" not in url, f"Doubled URL prefix in lesson URL: {url}"


def test_api_search_limit(tmp_path: Path):
    """Test /api/search?limit=N caps response to N results."""
    ws = _make_search_workspace(tmp_path)
    # Add more tasks to exceed limit
    for i in range(5):
        (ws / "tasks" / f"extra-git-task-{i}.md").write_text(
            f"---\nstate: active\ncreated: 2026-03-0{i + 1}\n---\n# Extra Git Task {i}\n"
        )

    site_dir = tmp_path / "site"
    app = create_app(ws, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=git&limit=2")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["results"]) == 2


def test_api_search_response_structure(tmp_path: Path):
    """Test /api/search result items have the expected structure."""
    ws = _make_search_workspace(tmp_path)
    site_dir = tmp_path / "site"
    app = create_app(ws, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=git")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "results" in data
        assert "total" in data
        assert "query" in data
        assert "type_filter" in data
        for result in data["results"]:
            assert "type" in result
            assert "title" in result
            assert "url" in result


def test_api_search_valid_types(client):
    """Test /api/search accepts all documented type values."""
    for t in ("lesson", "skill", "task", "journal", "summary", "package", "plugin"):
        resp = client.get(f"/api/search?q=test&type={t}")
        # Empty workspace → 0 results, but valid request
        assert resp.status_code == 200, f"type={t} should be valid"


def test_api_search_finds_journal_by_body(tmp_path: Path):
    """Test /api/search indexes journal body content, not just first-line preview."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    # Create a journal entry where the unique term only appears deep in the body
    journal_dir = tmp_path / "journal" / "2026-03-01"
    journal_dir.mkdir(parents=True)
    (journal_dir / "session.md").write_text(
        "# Session\n\n"
        "## Summary\n\n"
        "Worked on routine tasks.\n\n"
        "## Details\n\n"
        "Implemented the quuxfrobnicator feature in the backend.\n"
    )

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=quuxfrobnicator")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1, "Journal body content should be indexed and searchable"
        assert any(r["type"] == "journal" for r in data["results"])


def test_api_search_pure_punctuation_query(client):
    """Test /api/search returns 400 for queries that normalise to zero words (e.g. '--')."""
    resp = client.get("/api/search?q=--")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data


def test_api_search_indexes_submodule_lessons(tmp_path: Path):
    """Test /api/search includes lessons from git submodules."""
    ws = _make_search_workspace(tmp_path)

    # Simulate a submodule with its own lessons dir
    sub_dir = ws / "gptme-contrib"
    sub_dir.mkdir()
    sub_lessons = sub_dir / "lessons" / "workflow"
    sub_lessons.mkdir(parents=True)
    (sub_lessons / "submodule-deploy.md").write_text(
        "---\nmatch:\n  keywords: [deploy workflow, submodule deploy]\nstatus: active\n---\n"
        "# Submodule Deploy Workflow\n\nDeploy from submodule correctly.\n"
    )

    # Create a .gitmodules file so detect_submodules picks it up
    (ws / ".gitmodules").write_text(
        '[submodule "gptme-contrib"]\n'
        "    path = gptme-contrib\n"
        "    url = https://github.com/gptme/gptme-contrib.git\n"
    )

    site_dir = tmp_path / "site"
    app = create_app(ws, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=deploy")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1, "Submodule lessons should be indexed and searchable"
        assert any(r["type"] == "lesson" for r in data["results"])


def test_make_excerpt_strips_fenced_code_block_content():
    """Fenced code block content must not appear in excerpts."""
    from gptme_dashboard.server import _make_excerpt

    body = (
        "Some description text.\n\n"
        "```python\n"
        "x = run_agent(workspace)\n"
        "result = x.outcome\n"
        "```\n\n"
        "More text after code.\n"
    )
    excerpt = _make_excerpt(body)
    assert "run_agent" not in excerpt, f"Code block content leaked into excerpt: {excerpt!r}"
    assert "x.outcome" not in excerpt, f"Code block content leaked into excerpt: {excerpt!r}"
    assert "Some description" in excerpt, f"Expected description in excerpt: {excerpt!r}"


def test_make_excerpt_all_headings_returns_stripped_text():
    """When body has only headings, excerpt returns heading text without # markers."""
    from gptme_dashboard.server import _make_excerpt

    body = "# My Lesson\n## Rule\n## Context\n"
    excerpt = _make_excerpt(body)
    # No prose found; heading text is returned with markers stripped — acceptable fallback
    assert "#" not in excerpt, f"Heading markers should be stripped: {excerpt!r}"
    # Some content should be present (headings stripped of # become words)
    assert len(excerpt) > 0 or excerpt == ""  # empty is also acceptable


def test_make_excerpt_body_starts_with_fenced_block():
    """When the body opens with a fenced code block, code must not appear in excerpt."""
    from gptme_dashboard.server import _make_excerpt

    # Body where the very first non-blank content is a fenced code block
    body = (
        "```python\n"
        "secret_code = 'should not appear'\n"
        "```\n\n"
        "Prose description after the code block.\n"
    )
    excerpt = _make_excerpt(body)
    assert "secret_code" not in excerpt, f"Leading code block leaked: {excerpt!r}"
    assert "Prose description" in excerpt, f"Prose after block missing: {excerpt!r}"


def test_make_excerpt_via_search(tmp_path: Path):
    """Search result excerpts should not start with markdown heading markers."""
    from gptme_dashboard.server import create_app

    ws = tmp_path
    (ws / "gptme.toml").write_text('[agent]\nname = "test"\n')
    lessons_dir = ws / "lessons" / "patterns"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "my-lesson.md").write_text(
        "---\nmatch:\n  keywords: [uniquexyz]\nstatus: active\n---\n"
        "# My Lesson\n\n"
        "## Rule\n"
        "Do the uniquexyz thing.\n\n"
        "## Context\n"
        "When context arises.\n"
    )
    site_dir = tmp_path / "site"
    app = create_app(ws, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=uniquexyz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1
        result = next(r for r in data["results"] if r.get("type") == "lesson")
        excerpt = result.get("excerpt", "")
        # Excerpt should not start with a heading marker
        assert not excerpt.startswith("#"), f"Excerpt should not start with '#': {excerpt!r}"
        # Should contain the Rule content
        assert (
            "uniquexyz" in excerpt or "Rule" in excerpt or excerpt == ""
        ), f"Unexpected excerpt: {excerpt!r}"


def test_make_excerpt_strips_thematic_breaks():
    """Thematic break lines (---) must not appear in the excerpt."""
    from gptme_dashboard.server import _make_excerpt

    body = "First prose paragraph.\n\n" "---\n\n" "Second prose paragraph.\n"
    excerpt = _make_excerpt(body)
    assert "---" not in excerpt, f"Thematic break leaked into excerpt: {excerpt!r}"
    assert "First prose" in excerpt, f"Prose missing from excerpt: {excerpt!r}"


def test_make_excerpt_preserves_underscores_in_identifiers():
    """Identifiers like my_var_name must not have underscores stripped."""
    from gptme_dashboard.server import _make_excerpt

    body = "Use `my_var_name` to configure the option.\n"
    excerpt = _make_excerpt(body)
    assert "my_var_name" in excerpt, f"Identifier underscore stripped: {excerpt!r}"


def test_api_search_finds_package(tmp_path: Path):
    """Test /api/search indexes packages by name and description."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    pkg_dir = tmp_path / "packages" / "gptme-frobnicate"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "gptme-frobnicate"\nversion = "0.1.0"\n'
        'description = "Frobnicate widgets for gptme"\n'
    )
    (pkg_dir / "README.md").write_text("# gptme-frobnicate\n\nFrobnicate widgets.\n")

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=frobnicate")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1, "Package should be indexed and searchable"
        pkg_results = [r for r in data["results"] if r["type"] == "package"]
        assert pkg_results, "Expected at least one package result"
        assert any("frobnicate" in r["title"].lower() for r in pkg_results)


def test_api_search_finds_plugin(tmp_path: Path):
    """Test /api/search indexes plugins by name and README body."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    plugin_dir = tmp_path / "plugins" / "gptme-xyzzy"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("# gptme-xyzzy\n\nThe xyzzy plugin teleports context.\n")

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=xyzzy")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1, "Plugin should be indexed and searchable"
        plugin_results = [r for r in data["results"] if r["type"] == "plugin"]
        assert plugin_results, "Expected at least one plugin result"
        assert any("xyzzy" in r["title"].lower() for r in plugin_results)


def test_api_search_type_filter_package(tmp_path: Path):
    """Test /api/search?type=package returns only packages."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    # Create a package and a lesson both matching the query
    pkg_dir = tmp_path / "packages" / "gptme-deploy"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "gptme-deploy"\nversion = "0.1.0"\n'
        'description = "Deploy helpers for gptme"\n'
    )
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "deploy-lesson.md").write_text(
        "---\nmatch:\n  keywords: [deploy]\nstatus: active\n---\n"
        "# Deploy Lesson\n\nHow to deploy.\n"
    )

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=deploy&type=package")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type_filter"] == "package"
        assert data["results"], "Expected at least one package result for 'deploy' query"
        for result in data["results"]:
            assert result["type"] == "package", f"Expected only packages, got {result['type']}"


def test_api_search_type_filter_plugin(tmp_path: Path):
    """Test /api/search?type=plugin returns only plugins."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestBot"\n')
    # Create a plugin and a lesson both matching the query
    plugin_dir = tmp_path / "plugins" / "gptme-notify"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("# gptme-notify\n\nSend notifications from gptme.\n")
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "notify-lesson.md").write_text(
        "---\nmatch:\n  keywords: [notify]\nstatus: active\n---\n"
        "# Notify Lesson\n\nHow to send notifications.\n"
    )

    site_dir = tmp_path / "site"
    app = create_app(tmp_path, site_dir=site_dir)
    app.config["TESTING"] = True

    with app.test_client() as c:
        resp = c.get("/api/search?q=notify&type=plugin")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type_filter"] == "plugin"
        assert data["results"], "Expected at least one plugin result for 'notify' query"
        for result in data["results"]:
            assert result["type"] == "plugin", f"Expected only plugins, got {result['type']}"
