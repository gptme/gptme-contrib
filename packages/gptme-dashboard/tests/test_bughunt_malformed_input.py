"""Regression tests for malformed workspace input (frontmatter, session records)."""

import json
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gptme_dashboard.generate import (
    collect_workspace_data,
    scan_lessons,
    scan_skills,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))


# --- F1: null/non-string skill name crashes the whole generate --------------


def test_skill_with_empty_name_does_not_crash_collect(tmp_path: Path) -> None:
    """A skill with `name:` left empty (YAML null) next to a normal skill."""
    _write(tmp_path / "skills/a/SKILL.md", "---\nname: alpha\ndescription: A\n---\n# A\n")
    _write(tmp_path / "skills/b/SKILL.md", "---\nname:\ndescription: B\n---\n# B\n")
    data = collect_workspace_data(tmp_path)
    names = [g.get("name") for g in data["guidance"] if g["kind"] == "skill"]
    assert len(names) == 2


# --- F2: non-string skill description crashes scan_skills -------------------


def test_skill_with_list_description_does_not_crash(tmp_path: Path) -> None:
    _write(
        tmp_path / "skills/a/SKILL.md",
        "---\nname: alpha\ndescription:\n  - first point\n  - second point\n---\n# A\n",
    )
    skills = scan_skills(tmp_path)
    assert len(skills) == 1


# --- F3: non-mapping frontmatter crashes scan_lessons -----------------------


def test_lesson_with_scalar_frontmatter_does_not_crash(tmp_path: Path) -> None:
    _write(tmp_path / "lessons/workflow/x.md", "---\njust a note\n---\n# X\n\nbody\n")
    lessons = scan_lessons(tmp_path)
    assert len(lessons) == 1


# --- F4: /api/sessions?model=... 500s when any store record has model=null ---


@pytest.fixture
def null_model_client(tmp_path: Path):
    pytest.importorskip("flask")
    pytest.importorskip("gptme_sessions")
    from gptme_dashboard.server import create_app

    (tmp_path / "lessons").mkdir()
    sessions_dir = tmp_path / "state" / "sessions"
    sessions_dir.mkdir(parents=True)
    ts = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT10:00:00Z")
    records = [
        {
            "session_id": "a",
            "timestamp": ts,
            "harness": "gptme",
            "model": "claude-opus-4-6",
            "outcome": "productive",
            "duration_seconds": 60,
        },
        {
            "session_id": "b",
            "timestamp": ts,
            "harness": None,
            "model": None,
            "outcome": "noop",
            "duration_seconds": 60,
        },
    ]
    with open(sessions_dir / "session-records.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)
    app = create_app(tmp_path, site_dir=tmp_path / "site")
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_sessions_model_filter_with_null_model_record(null_model_client) -> None:
    resp = null_model_client.get("/api/sessions?model=claude-opus-4-6")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["total"] == 1


def test_sessions_harness_filter_with_null_harness_record(null_model_client) -> None:
    resp = null_model_client.get("/api/sessions?harness=gptme")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["total"] == 1
