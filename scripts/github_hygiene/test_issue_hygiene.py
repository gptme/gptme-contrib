"""Tests for issue_hygiene orchestrator.

These tests cover the two things the prototype has to get right before it
touches a real repo:

1. The idempotency marker matches exactly what `build_comment` emits.
2. The prompt template renders every expected field, including truncation and
   empty-body handling, without raising KeyError.

External I/O (`gh`, `gptme`) is never exercised here — those are thin
subprocess wrappers and will be exercised in the staged rollout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import issue_hygiene  # type: ignore[import-not-found]  # noqa: E402,I001


REPO = "gptme/gptme-contrib"
TEMPLATE_PATH = Path(__file__).parent / "prompts" / "issue-hygiene.md"


def _issue(**overrides):
    base = dict(
        number=42,
        title="gptme crashes on empty prompt",
        body="Steps:\n1. Run gptme\n2. Hit enter\n\nCrash.",
        author="alice",
        labels=["bug"],
    )
    base.update(overrides)
    return issue_hygiene.Issue(**base)


def _recent(n: int = 3):
    return [
        issue_hygiene.RecentIssue(number=10 + i, title=f"example issue {i}")
        for i in range(n)
    ]


def test_marker_is_stable_and_hidden_in_comment():
    body = issue_hygiene.build_comment("- Missing gptme version")
    assert issue_hygiene.MARKER in body
    # Marker must be an HTML comment so GitHub does not render it.
    assert body.strip().startswith("<!--")
    # No duplicate markers (idempotency depends on this).
    assert body.count(issue_hygiene.MARKER) == 1


def test_marker_is_versioned():
    # If we break the schema we should bump the version; leaving it at v1
    # without intent would silently re-trigger on already-processed issues.
    assert issue_hygiene.MARKER.endswith("v1 -->")


def test_render_prompt_substitutes_every_placeholder():
    template = TEMPLATE_PATH.read_text()
    rendered = issue_hygiene.render_prompt(
        template,
        repo=REPO,
        issue=_issue(),
        recent=_recent(),
    )
    for placeholder in (
        "{repo}",
        "{issue_number}",
        "{issue_title}",
        "{issue_author}",
        "{issue_labels}",
        "{issue_body}",
        "{recent_issues}",
    ):
        assert placeholder not in rendered, f"unfilled placeholder: {placeholder}"

    assert REPO in rendered
    assert "42" in rendered
    assert "gptme crashes on empty prompt" in rendered
    assert "@alice" in rendered
    assert "- #10: example issue 0" in rendered


def test_render_prompt_handles_empty_body_and_no_labels():
    template = TEMPLATE_PATH.read_text()
    rendered = issue_hygiene.render_prompt(
        template,
        repo=REPO,
        issue=_issue(body="", labels=[]),
        recent=[],
    )
    assert "(empty body)" in rendered
    assert "(none)" in rendered  # labels and recent both fall back to (none)


def test_render_prompt_does_not_crash_on_format_conflicts():
    # Issue bodies often contain `{` and `}` (e.g. JSON snippets). The renderer
    # must not interpret those as format placeholders.
    template = TEMPLATE_PATH.read_text()
    tricky = _issue(body='{"error": "unexpected {bracket}"}')
    rendered = issue_hygiene.render_prompt(template, repo=REPO, issue=tricky, recent=[])
    assert '"error": "unexpected {bracket}"' in rendered


def test_build_comment_contains_warning_framing():
    body = issue_hygiene.build_comment("- Possible duplicate of #1")
    assert "warning-only" in body.lower()
    assert "- Possible duplicate of #1" in body


def test_skip_token_matches_template_contract():
    template = TEMPLATE_PATH.read_text()
    assert issue_hygiene.SKIP_TOKEN in template, (
        "SKIP_TOKEN must match the exact string the prompt tells the model "
        "to emit when nothing is wrong."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
