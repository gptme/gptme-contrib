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


def test_reviews_preserve_operator_and_exclude_actual_pr_author():
    payload = [
        {
            "number": 7,
            "title": "A change",
            "url": "https://example.test/pr/7",
            "author": {"login": "NewAgent"},
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
    assert [review.reviewer for review in reviews] == ["ErikBjare", "AnotherReviewer"]
    cmd = run.call_args.args[0]
    assert "author" in cmd[cmd.index("--json") + 1].split(",")
