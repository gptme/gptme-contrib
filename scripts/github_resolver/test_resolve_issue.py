"""Tests for resolve_issue orchestrator.

These cover the pure parts the prototype must get right before it is pointed
at a real repo: the stable idempotency marker on comments, the prompt
template round-trip, and the `RESOLVER_STATUS`/`RESOLVER_SUMMARY` parser.

External I/O (`gh`, `gptme`, `git`) is stubbed — those are thin subprocess
wrappers exercised in the staged rollout, not in unit tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import resolve_issue  # type: ignore[import-not-found]  # noqa: E402,I001


REPO = "gptme/gptme-contrib"
TEMPLATE_PATH = Path(__file__).parent / "prompts" / "issue-resolver.md"


def _issue(**overrides):
    base = dict(
        number=42,
        title="gptme crashes on empty prompt",
        body="Steps:\n1. Run gptme\n2. Hit enter\n\nCrash.",
        author="alice",
        labels=["bug"],
    )
    base.update(overrides)
    return resolve_issue.Issue(**base)


def test_marker_is_stable_and_versioned():
    assert resolve_issue.MARKER_COMMENT.startswith("<!--")
    assert resolve_issue.MARKER_COMMENT.endswith("v1 -->")


def test_branch_name_is_deterministic():
    assert resolve_issue.branch_for_issue(42) == "gptme-resolver/issue-42"
    assert resolve_issue.branch_for_issue(1) == "gptme-resolver/issue-1"


def test_render_prompt_substitutes_every_placeholder():
    template = TEMPLATE_PATH.read_text()
    rendered = resolve_issue.render_prompt(template, repo=REPO, issue=_issue())
    for placeholder in (
        "{repo}",
        "{issue_number}",
        "{issue_title}",
        "{issue_author}",
        "{issue_labels}",
        "{issue_body}",
    ):
        assert placeholder not in rendered, f"unfilled placeholder: {placeholder}"
    assert REPO in rendered
    assert "42" in rendered
    assert "gptme crashes on empty prompt" in rendered
    assert "@alice" in rendered


def test_render_prompt_handles_empty_body_and_no_labels():
    template = TEMPLATE_PATH.read_text()
    rendered = resolve_issue.render_prompt(
        template, repo=REPO, issue=_issue(body="", labels=[])
    )
    assert "(empty body)" in rendered
    assert "(none)" in rendered


def test_render_prompt_does_not_crash_on_format_conflicts():
    # Issue bodies often contain `{` / `}` (JSON snippets, f-string examples).
    # The renderer must not treat those as format placeholders.
    template = TEMPLATE_PATH.read_text()
    tricky = _issue(body='{"error": "unexpected {bracket}"}')
    rendered = resolve_issue.render_prompt(template, repo=REPO, issue=tricky)
    assert '"error": "unexpected {bracket}"' in rendered


def test_parse_status_changes_extracts_summary():
    out = (
        "... work ...\n"
        "RESOLVER_STATUS: changes\n"
        "RESOLVER_SUMMARY: Fixed the crash by guarding the empty-prompt branch.\n"
    )
    status, message = resolve_issue.parse_status(out)
    assert status == "changes"
    assert "empty-prompt" in message


def test_parse_status_no_changes_extracts_reason():
    out = (
        "RESOLVER_STATUS: no_changes\n"
        "RESOLVER_REASON: Needs product direction on retry semantics.\n"
    )
    status, message = resolve_issue.parse_status(out)
    assert status == "no_changes"
    assert message.startswith("Needs product direction")


def test_parse_status_missing_marker_returns_error():
    out = "model chatted about the issue but forgot the marker.\n"
    status, message = resolve_issue.parse_status(out)
    assert status == "error"
    assert "RESOLVER_STATUS" in message


def test_parse_status_is_last_line_tolerant():
    # The marker can appear anywhere — we anchor on the line, not the tail.
    out = (
        "pre-amble\n"
        "RESOLVER_STATUS: changes\n"
        "RESOLVER_SUMMARY: tightened regex\n"
        "post-amble junk\n"
    )
    status, message = resolve_issue.parse_status(out)
    assert status == "changes"
    assert message == "tightened regex"


def test_write_output_creates_dir(tmp_path):
    resolve_issue.write_output(
        tmp_path / "out", "status.json", json.dumps({"ok": True})
    )
    assert (tmp_path / "out" / "status.json").exists()


def test_prompt_template_mentions_both_markers():
    # The prompt contract itself has to advertise both markers or the model
    # will never emit them — guard against silent template rot.
    template = TEMPLATE_PATH.read_text()
    assert "RESOLVER_STATUS: changes" in template
    assert "RESOLVER_STATUS: no_changes" in template
    assert "RESOLVER_SUMMARY" in template
    assert "RESOLVER_REASON" in template


def test_status_regex_ignores_prose_mentions():
    # The model often echoes the words "RESOLVER_STATUS" in the middle of a
    # sentence while explaining what it's about to do. Only an anchored line
    # of the exact form should count.
    prose = "I'll now emit RESOLVER_STATUS: changes inline like this."
    status, _ = resolve_issue.parse_status(prose)
    assert status == "error"


def test_open_draft_pr_retrigger_returns_existing_url(monkeypatch):
    # On re-trigger, `gh pr create` exits 1 ("a pull request … already exists").
    # open_draft_pr must catch CalledProcessError and return the existing PR URL
    # via `gh pr view` so the caller can still post the issue comment.
    import subprocess

    calls: list[list[str]] = []

    def fake_gh(args, *, check=True):
        calls.append(args)
        if args[0] == "pr" and args[1] == "create":
            raise subprocess.CalledProcessError(1, "gh")
        if args[0] == "pr" and args[1] == "view":
            return "https://github.com/gptme/gptme-contrib/pull/99\n"
        return ""

    monkeypatch.setattr(resolve_issue, "gh", fake_gh)
    url = resolve_issue.open_draft_pr(
        "gptme/gptme-contrib", 42, "gptme-resolver/issue-42", "fixed the crash"
    )
    assert url == "https://github.com/gptme/gptme-contrib/pull/99"
    # Verify we fell back to `gh pr view`
    assert any(a[0] == "pr" and a[1] == "view" for a in calls)
