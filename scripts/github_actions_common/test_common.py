"""Tests for the shared GitHub Actions helpers.

No network or subprocess: every test stubs the `gh` CLI by patching the
internal `subprocess.run` reference.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

# Make the package importable when running directly: `pytest test_common.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_actions_common import (  # noqa: E402
    MAX_BODY_CHARS,
    Issue,
    fetch_issue,
    has_marker_comment,
    write_output,
)


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=0, stdout=stdout, stderr=""
    )


def test_fetch_issue_truncates_long_bodies() -> None:
    long_body = "x" * (MAX_BODY_CHARS + 100)
    payload = {
        "number": 42,
        "title": "Bug",
        "body": long_body,
        "author": {"login": "alice"},
        "labels": [{"name": "bug"}, {"name": "triage"}],
    }
    with mock.patch(
        "github_actions_common.subprocess.run",
        return_value=_completed(json.dumps(payload)),
    ) as run:
        issue = fetch_issue("owner/repo", 42)
    run.assert_called_once()
    assert isinstance(issue, Issue)
    assert issue.number == 42
    assert issue.author == "alice"
    assert issue.labels == ["bug", "triage"]
    assert issue.body.endswith("…(truncated)")
    assert len(issue.body) == MAX_BODY_CHARS + len("\n…(truncated)")


def test_fetch_issue_handles_missing_author() -> None:
    payload = {
        "number": 1,
        "title": "x",
        "body": "",
        "author": None,
        "labels": [],
    }
    with mock.patch(
        "github_actions_common.subprocess.run",
        return_value=_completed(json.dumps(payload)),
    ):
        issue = fetch_issue("owner/repo", 1)
    assert issue.author == "unknown"
    assert issue.body == ""
    assert issue.labels == []


def test_has_marker_comment_true() -> None:
    payload = {
        "comments": [
            {"body": "Some random comment"},
            {"body": "<!-- gptme-issue-hygiene: v1 -->\nactual hygiene"},
        ]
    }
    marker = "<!-- gptme-issue-hygiene: v1 -->"
    with mock.patch(
        "github_actions_common.subprocess.run",
        return_value=_completed(json.dumps(payload)),
    ):
        assert has_marker_comment("owner/repo", 1, marker) is True


def test_has_marker_comment_false_when_marker_missing() -> None:
    payload = {"comments": [{"body": "ok"}, {"body": None}]}
    marker = "<!-- gptme-issue-hygiene: v1 -->"
    with mock.patch(
        "github_actions_common.subprocess.run",
        return_value=_completed(json.dumps(payload)),
    ):
        assert has_marker_comment("owner/repo", 1, marker) is False


def test_has_marker_comment_isolates_versions() -> None:
    """A v1 marker check must not match a v2 comment, and vice versa."""
    payload = {"comments": [{"body": "<!-- gptme-issue-hygiene: v2 -->\n..."}]}
    marker_v1 = "<!-- gptme-issue-hygiene: v1 -->"
    with mock.patch(
        "github_actions_common.subprocess.run",
        return_value=_completed(json.dumps(payload)),
    ):
        assert has_marker_comment("owner/repo", 1, marker_v1) is False


def test_write_output_creates_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "output"
    write_output(target, "status.txt", "ok")
    assert (target / "status.txt").read_text() == "ok"
