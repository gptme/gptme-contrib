"""Tests for the dynamic dashboard server."""

import json
import textwrap
import unittest.mock
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
    """Test /api/services/logs returns empty on non-Linux."""
    with unittest.mock.patch("platform.system", return_value="Darwin"):
        resp = client.get("/api/services/logs?service=gptme-test.service")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["logs"] == []
    assert data["platform"] == "Darwin"


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
