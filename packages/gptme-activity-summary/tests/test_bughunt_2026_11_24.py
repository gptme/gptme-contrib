"""Regression tests for gptme-activity-summary bug-hunt fixes (2026-11-24)."""

import json
from datetime import date
from unittest.mock import patch

from gptme_activity_summary import github_data
from gptme_activity_summary.workspace_data import fetch_tweets, fetch_workspace_activity


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_one_malformed_tweet_yaml_does_not_abort_workspace_activity(tmp_path):
    """A single unparseable tweet file must be skipped, not crash the fetch.

    fetch_workspace_activity is called unguarded from cli._build_extra_context,
    so an exception here aborts the whole daily/weekly/monthly summary.
    """
    posted = tmp_path / "tweets" / "posted"
    _write(
        posted / "tweet_20261124_good.yml",
        "created_at: '2026-11-24T10:00:00'\ntext: shipped the thing\ntype: tweet\n",
    )
    # Unterminated quote -> yaml.scanner.ScannerError
    _write(
        posted / "tweet_20261124_bad.yml",
        "created_at: '2026-11-24T11:00:00'\ntext: \"it's: broken\ntype: tweet\n",
    )
    activity = fetch_workspace_activity(date(2026, 11, 24), date(2026, 11, 24), tmp_path)
    assert [t.text for t in activity.tweets] == ["shipped the thing"]


def test_tweet_yaml_with_non_mapping_top_level_is_skipped(tmp_path):
    posted = tmp_path / "tweets" / "posted"
    _write(posted / "tweet_20261124_list.yml", "- just\n- a list\n")
    _write(
        posted / "tweet_20261124_ok.yml",
        "created_at: '2026-11-24T10:00:00'\ntext: hello\n",
    )
    tweets = fetch_tweets(date(2026, 11, 24), date(2026, 11, 24), tmp_path)
    assert [t.text for t in tweets] == ["hello"]


def test_reviews_received_excludes_reviews_submitted_outside_window(monkeypatch):
    """A PR updated in-window carries ALL its historical reviews; only reviews
    submitted within [start, end] are 'received' in that period."""
    monkeypatch.delenv("BOT_USERNAME", raising=False)
    prs = [
        {
            "number": 7,
            "title": "Long-running PR",
            "url": "https://github.com/o/r/pull/7",
            "author": {"login": "bob"},
            "reviews": [
                {
                    "author": {"login": "erik"},
                    "state": "COMMENTED",
                    "submittedAt": "2026-09-01T12:00:00Z",
                },
                {
                    "author": {"login": "alice"},
                    "state": "APPROVED",
                    "submittedAt": "2026-11-24T12:00:00Z",
                },
            ],
        }
    ]
    with patch.object(github_data, "_run_command", return_value=json.dumps(prs)):
        reviews = github_data.get_reviews_received(
            date(2026, 11, 24), date(2026, 11, 24), ["o/r"], author="bob"
        )
    assert [r.reviewer for r in reviews] == ["alice"]
