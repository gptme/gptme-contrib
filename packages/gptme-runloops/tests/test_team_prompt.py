"""Tests for the autonomous-team coordinator prompt."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from gptme_runloops.team import TEAM_PROMPT_TEMPLATE, TeamRun


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Workspace with a minimal gptme.toml naming the agent."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "alice"\n')
    return tmp_path


def test_team_prompt_fills_current_time(workspace: Path) -> None:
    """The rendered prompt must not contain a literal template placeholder.

    Regression: the template carried ``{{timestamp}}`` and the only format call
    passed ``agent_name``, so every coordinator prompt literally read
    ``**Current Time**: {timestamp}``.
    """
    before = datetime.now().astimezone()
    prompt = TeamRun(workspace=workspace).generate_prompt()
    after = datetime.now().astimezone()

    assert "{timestamp}" not in prompt
    assert "**Current Time**: " in prompt

    rendered = prompt.split("**Current Time**: ", 1)[1].splitlines()[0]
    parsed = datetime.fromisoformat(rendered)
    assert before <= parsed <= after


def test_team_prompt_fills_agent_name(workspace: Path) -> None:
    prompt = TeamRun(workspace=workspace).generate_prompt()
    assert prompt.startswith("You are alice, running in **autonomous-team")


def test_template_has_no_unfilled_placeholders(workspace: Path) -> None:
    """Guard the whole template, not just the timestamp line."""
    prompt = TeamRun(workspace=workspace).generate_prompt()
    for placeholder in ("{agent_name}", "{current_time}", "{timestamp}"):
        assert placeholder not in prompt


def test_custom_template_is_used_verbatim(workspace: Path) -> None:
    """A workspace-provided template bypasses the built-in one entirely."""
    custom = workspace / "scripts" / "runs" / "team"
    custom.mkdir(parents=True)
    (custom / "team-prompt.txt").write_text("custom {timestamp} kept literally")

    prompt = TeamRun(workspace=workspace).generate_prompt()
    assert prompt == "custom {timestamp} kept literally"
    assert TEAM_PROMPT_TEMPLATE.format(agent_name="x", current_time="now") != prompt
