"""Generic summaries must follow the deployment's GitHub identity (#1705)."""

import json
from datetime import date
from unittest.mock import patch

import pytest

from gptme_activity_summary.github_data import get_cross_repo_prs, get_reviews_received


@pytest.mark.parametrize(
    ("configured", "explicit", "expected"),
    [(None, None, "@me"), ("NewAgent", None, "NewAgent"), ("NewAgent", "OtherUser", "OtherUser")],
)
def test_cross_repo_search_uses_deployment_author(monkeypatch, configured, explicit, expected):
    if configured:
        monkeypatch.setenv("BOT_USERNAME", configured)
    else:
        monkeypatch.delenv("BOT_USERNAME", raising=False)
    with patch("gptme_activity_summary.github_data._run_command", return_value="[]") as run:
        get_cross_repo_prs(date(2026, 10, 1), date(2026, 10, 2), author=explicit)
    cmd = run.call_args.args[0]
    assert cmd[cmd.index("--author") + 1] == expected


@pytest.mark.parametrize(
    ("configured", "explicit", "expected"),
    [(None, None, "@me"), ("NewAgent", None, "NewAgent"), ("NewAgent", "OtherUser", "OtherUser")],
)
def test_reviews_received_uses_deployment_author(monkeypatch, configured, explicit, expected):
    if configured:
        monkeypatch.setenv("BOT_USERNAME", configured)
    else:
        monkeypatch.delenv("BOT_USERNAME", raising=False)
    with patch("gptme_activity_summary.github_data._run_command", return_value="[]") as run:
        get_reviews_received(
            date(2026, 10, 1), date(2026, 10, 2), ["NewAgent/brain"], author=explicit
        )
    cmd = run.call_args.args[0]
    assert cmd[cmd.index("--author") + 1] == expected


def test_reviews_received_author_stable_across_repos(monkeypatch):
    """Reviewer logins must not overwrite the query author across repos.

    With two repos, the first PR's reviewer (ErikBjare) must not bleed into
    the --author flag for the second repo's command.
    """
    monkeypatch.setenv("BOT_USERNAME", "NewAgent")
    payload_with_review = json.dumps(
        [
            {
                "number": 1,
                "title": "A change",
                "url": "https://example.test/pr/1",
                "author": {"login": "NewAgent"},
                "reviews": [{"author": {"login": "ErikBjare"}}],
            }
        ]
    )
    calls: list[list[str]] = []

    def capture(cmd):
        calls.append(cmd)
        return payload_with_review

    with patch("gptme_activity_summary.github_data._run_command", side_effect=capture):
        get_reviews_received(
            date(2026, 10, 1), date(2026, 10, 2), ["NewAgent/brain", "NewAgent/tools"]
        )
    assert len(calls) == 2, "expected one command per repo"
    for cmd in calls:
        assert cmd[cmd.index("--author") + 1] == "NewAgent"


def test_reviews_null_review_author_does_not_raise():
    payload = [
        {
            "number": 8,
            "title": "Another change",
            "url": "https://example.test/pr/8",
            "author": {"login": "NewAgent"},
            "reviews": [{"author": None}, {"author": {"login": "ErikBjare"}}],
        }
    ]
    with patch("gptme_activity_summary.github_data._run_command", return_value=json.dumps(payload)):
        reviews = get_reviews_received(date(2026, 10, 1), date(2026, 10, 2), ["NewAgent/brain"])
    assert [r.reviewer for r in reviews] == ["ErikBjare"]


@pytest.mark.parametrize("pr_author", [{"login": "NewAgent"}, None])
def test_reviews_preserve_operator_and_exclude_actual_pr_author(pr_author):
    payload = [
        {
            "number": 7,
            "title": "A change",
            "url": "https://example.test/pr/7",
            "author": pr_author,
            "reviews": [
                {"author": {"login": "NewAgent"}},
                {"author": {"login": "ErikBjare"}},
                {"author": {"login": "AnotherReviewer"}},
            ],
        }
    ]
    with patch(
        "gptme_activity_summary.github_data._run_command", return_value=json.dumps(payload)
    ) as run:
        reviews = get_reviews_received(date(2026, 10, 1), date(2026, 10, 2), ["NewAgent/brain"])
    expected = ["ErikBjare", "AnotherReviewer"]
    if pr_author is None:
        expected.insert(0, "NewAgent")
    assert [review.reviewer for review in reviews] == expected
    cmd = run.call_args.args[0]
    assert "author" in cmd[cmd.index("--json") + 1].split(",")
