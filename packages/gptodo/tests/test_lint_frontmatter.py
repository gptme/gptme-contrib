"""Tests for the `gptodo lint` command and lint_frontmatter_fields helper.

Gordon 2026-07-01 upstream fix: the autonomous loop invents plausible-sounding
frontmatter fields under pressure (e.g. `modified`, an operator-hallucinated
suggestion in a Jul-01 session that Erik correctly rejected as an
anti-design-goal). This exercise verifies:

  1. Known-good frontmatter produces no findings.
  2. `modified` is flagged as deprecated/anti-design-goal with the specific
     alternative (mtime / git log) in the message.
  3. Other deprecated hallucinated fields (last_modified, updated_at) also
     get called out with alternatives.
  4. Truly unknown fields get a "warn-unknown" finding.
  5. `gptodo lint` prints findings, `--json` returns structured output,
     `--strict` sets non-zero exit.
  6. The lint is warn-only: it never rejects the task at load time.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.utils import (
    DEPRECATED_FRONTMATTER_FIELDS,
    KNOWN_FRONTMATTER_FIELDS,
    lint_frontmatter_fields,
    load_tasks,
)


def _write(tasks_dir: Path, name: str, body: str) -> None:
    (tasks_dir / f"{name}.md").write_text(body)


CLEAN_TASK = """\
---
state: todo
created: 2026-06-01T00:00:00+00:00
priority: high
task_type: action
tags: [infra]
---
# Clean Task
"""

TASK_WITH_MODIFIED = """\
---
state: todo
created: 2026-06-01T00:00:00+00:00
modified: 2026-06-15T12:00:00+00:00
---
# Task with modified (anti-design-goal)
"""

TASK_WITH_UNKNOWN = """\
---
state: todo
created: 2026-06-01T00:00:00+00:00
fabricated_field: some-value
---
# Task with unknown field
"""

TASK_WITH_INLINE_HASH = """\
---
state: todo
created: 2026-06-01T00:00:00+00:00
waiting_for: PR #815 review
---
# Task with inline hash
"""

TASK_WITH_MULTIPLE_BAD_FIELDS = """\
---
state: todo
created: 2026-06-01T00:00:00+00:00
modified: 2026-06-15
last_modified: 2026-06-15
updated_at: 2026-06-15
completely_made_up: yes
---
# Task with several hallucinated fields
"""


# -- unit tests on the helper ------------------------------------------------


def test_lint_helper_returns_empty_for_clean_metadata() -> None:
    metadata = {
        "state": "todo",
        "created": "2026-06-01T00:00:00+00:00",
        "priority": "high",
        "tags": ["infra"],
    }
    assert lint_frontmatter_fields(metadata) == []


def test_lint_helper_flags_modified_as_deprecated() -> None:
    metadata = {"state": "todo", "modified": "2026-06-15"}
    findings = lint_frontmatter_fields(metadata)
    assert len(findings) == 1
    severity, field, message = findings[0]
    assert severity == "warn-deprecated"
    assert field == "modified"
    # The message must point at the correct alternatives.
    assert "getmtime" in message or "mtime" in message
    assert "git log" in message
    assert "anti-design" in message.lower()


def test_lint_helper_flags_all_known_deprecated_fields() -> None:
    """Every field in DEPRECATED_FRONTMATTER_FIELDS should get flagged."""
    for field_name in DEPRECATED_FRONTMATTER_FIELDS:
        metadata = {"state": "todo", field_name: "some-value"}
        findings = lint_frontmatter_fields(metadata)
        deprecated = [f for f in findings if f[0] == "warn-deprecated"]
        assert len(deprecated) == 1, f"{field_name} should produce exactly one deprecated warning"
        assert deprecated[0][1] == field_name


def test_lint_helper_flags_unknown_field() -> None:
    metadata = {"state": "todo", "definitely_not_a_real_field": "x"}
    findings = lint_frontmatter_fields(metadata)
    assert len(findings) == 1
    severity, field, message = findings[0]
    assert severity == "warn-unknown"
    assert field == "definitely_not_a_real_field"
    assert "KNOWN_FRONTMATTER_FIELDS" in message


def test_lint_helper_multiple_findings() -> None:
    metadata = {
        "state": "todo",
        "modified": "x",
        "last_modified": "y",
        "some_random_field": "z",
    }
    findings = lint_frontmatter_fields(metadata)
    assert len(findings) == 3
    severities_by_field = {field: sev for sev, field, _ in findings}
    assert severities_by_field["modified"] == "warn-deprecated"
    assert severities_by_field["last_modified"] == "warn-deprecated"
    assert severities_by_field["some_random_field"] == "warn-unknown"


# -- CLI tests ---------------------------------------------------------------


def test_lint_cli_clean_workspace(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "clean", CLEAN_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["lint"])

    assert result.exit_code == 0, result.output
    assert "No frontmatter schema violations" in result.output


def test_lint_cli_flags_modified_with_alternative(tmp_path: Path, monkeypatch) -> None:
    """The specific case Erik flagged: `modified` field with the anti-design rationale."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "bad-task", TASK_WITH_MODIFIED)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["lint"])

    assert result.exit_code == 0, result.output
    assert "modified" in result.output
    assert "deprecated" in result.output.lower()
    # The specific alternative must be in the output
    assert "git log" in result.output or "getmtime" in result.output


def test_lint_cli_json_output(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "bad-task", TASK_WITH_MODIFIED)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["lint", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["findings"][0]["field"] == "modified"
    assert payload["findings"][0]["severity"] == "warn-deprecated"
    assert payload["findings"][0]["task"] == "bad-task"


def test_lint_cli_strict_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "bad-task", TASK_WITH_MODIFIED)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["lint", "--strict"])

    assert result.exit_code == 1


def test_lint_cli_strict_clean_exits_zero(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "clean", CLEAN_TASK)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["lint", "--strict"])

    assert result.exit_code == 0, result.output


def test_lint_cli_renders_schema_warning_as_schema_warning(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "inline-hash", TASK_WITH_INLINE_HASH)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["lint"])

    assert result.exit_code == 0, result.output
    assert "schema warning" in result.output
    assert "unknown field" not in result.output


def test_lint_cli_does_not_break_task_loading(tmp_path: Path, monkeypatch) -> None:
    """Warn-only nudge: bad fields must not prevent a task from loading."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "still-loadable", TASK_WITH_MULTIPLE_BAD_FIELDS)

    tasks = load_tasks(tasks_dir)
    assert len(tasks) == 1
    task = tasks[0]
    # All bad fields must still be present in metadata (we don't strip them)
    assert task.metadata["modified"] is not None
    assert task.metadata["last_modified"] is not None
    # YAML parses "yes" as bool True — the specific value doesn't matter,
    # just that the field survived the load
    assert "completely_made_up" in task.metadata
    # And the state field still works normally
    assert task.state == "todo"


def test_lint_cli_multiple_findings_per_task(tmp_path: Path, monkeypatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _write(tasks_dir, "many-bad", TASK_WITH_MULTIPLE_BAD_FIELDS)

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["lint", "--json"])

    payload = json.loads(result.output)
    # 3 deprecated (modified, last_modified, updated_at) + 1 unknown = 4
    assert payload["count"] == 4
    field_names = {f["field"] for f in payload["findings"]}
    assert field_names == {
        "modified",
        "last_modified",
        "updated_at",
        "completely_made_up",
    }


def test_known_fields_include_all_documented_ones() -> None:
    """Sanity: KNOWN_FRONTMATTER_FIELDS covers the fields the CLI documents.

    Guards against silent schema drift: if someone adds a new --set field
    to `gptodo edit` without also adding it to KNOWN_FRONTMATTER_FIELDS,
    every task using it will get a spurious warn-unknown lint finding.
    """
    # Fields the edit command explicitly supports via VALID_FIELDS
    expected_in_schema = {
        "state",
        "created",
        "priority",
        "task_type",
        "assigned_to",
        "waiting_since",
        "wait",
        "next_action",
        "waiting_for",
        "recur",
        "parent",
        "success_criterion",
        "tracking_issue",
        "upstream_coordination_id",
        "tags",
        "depends",
        "requires",
        "related",
        "discovered-from",
        "output_types",
        "tracking",
    }
    missing = expected_in_schema - KNOWN_FRONTMATTER_FIELDS
    assert not missing, (
        f"gptodo edit exposes fields not in KNOWN_FRONTMATTER_FIELDS: {missing}. "
        "Add them to gptodo/utils.py to avoid spurious lint warnings."
    )
