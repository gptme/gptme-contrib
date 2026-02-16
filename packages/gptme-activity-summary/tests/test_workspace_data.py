"""Tests for workspace_data module (tweets and emails)."""

from datetime import date, datetime
from pathlib import Path

from gptme_activity_summary.workspace_data import (
    PostedTweet,
    SentEmail,
    WorkspaceActivity,
    fetch_emails,
    fetch_tweets,
    fetch_workspace_activity,
    format_workspace_activity_for_prompt,
)


def test_fetch_tweets_nonexistent_dir(tmp_path: Path) -> None:
    """Returns empty list when tweets dir doesn't exist."""
    result = fetch_tweets(date(2025, 1, 1), date(2025, 1, 31), tmp_path)
    assert result == []


def test_fetch_tweets_with_data(tmp_path: Path) -> None:
    """Parses tweet YAML files correctly."""
    tweets_dir = tmp_path / "tweets" / "posted"
    tweets_dir.mkdir(parents=True)

    # Write a tweet file
    (tweets_dir / "tweet_20250115_test.yml").write_text(
        """created_at: '2025-01-15T10:30:00'
text: 'Hello world! Testing the tweet parser.'
type: tweet
in_reply_to: null
"""
    )

    # Write a reply file
    (tweets_dir / "reply_20250115_reply_123.yml").write_text(
        """created_at: '2025-01-15T11:00:00'
text: 'Great point!'
type: reply
in_reply_to: '123'
"""
    )

    # Write a tweet outside the date range
    (tweets_dir / "tweet_20250201_outside.yml").write_text(
        """created_at: '2025-02-01T10:00:00'
text: 'This should be excluded.'
type: tweet
in_reply_to: null
"""
    )

    result = fetch_tweets(date(2025, 1, 1), date(2025, 1, 31), tmp_path)
    assert len(result) == 2
    # reply_* sorts before tweet_* alphabetically
    assert result[0].text == "Great point!"
    assert result[0].tweet_type == "reply"
    assert result[1].text == "Hello world! Testing the tweet parser."
    assert result[1].tweet_type == "tweet"


def test_fetch_emails_nonexistent_dir(tmp_path: Path) -> None:
    """Returns empty list when email dir doesn't exist."""
    result = fetch_emails(date(2025, 1, 1), date(2025, 1, 31), tmp_path)
    assert result == []


def test_fetch_emails_with_data(tmp_path: Path) -> None:
    """Parses MIME-formatted email files correctly."""
    email_dir = tmp_path / "email" / "sent"
    email_dir.mkdir(parents=True)

    (email_dir / "test-email-1.md").write_text(
        """MIME-Version: 1.0
From: bob@example.com
To: Erik <erik@example.com>
Date: Wed, 15 Jan 2025 10:30:00 +0000
Subject: Test email
Message-ID: <test-1>

Hello Erik, this is a test email about the project status.
"""
    )

    # Email outside date range
    (email_dir / "test-email-2.md").write_text(
        """MIME-Version: 1.0
From: bob@example.com
To: Someone <someone@example.com>
Date: Mon, 03 Feb 2025 10:30:00 +0000
Subject: Outside range

This should not be included.
"""
    )

    result = fetch_emails(date(2025, 1, 1), date(2025, 1, 31), tmp_path)
    assert len(result) == 1
    assert result[0].subject == "Test email"
    assert result[0].to == "Erik <erik@example.com>"
    assert "test email" in result[0].body.lower()


def test_fetch_workspace_activity(tmp_path: Path) -> None:
    """fetch_workspace_activity combines tweets and emails."""
    # Create both dirs but leave them empty
    (tmp_path / "tweets" / "posted").mkdir(parents=True)
    (tmp_path / "email" / "sent").mkdir(parents=True)

    activity = fetch_workspace_activity(date(2025, 1, 1), date(2025, 1, 31), tmp_path)
    assert activity.tweets == []
    assert activity.emails == []


def test_format_workspace_activity_empty() -> None:
    """Empty activity returns empty string."""
    activity = WorkspaceActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
    )
    assert format_workspace_activity_for_prompt(activity) == ""


def test_format_workspace_activity_with_tweets() -> None:
    """Formats tweet activity as markdown."""
    activity = WorkspaceActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        tweets=[
            PostedTweet(
                text="Hello world!",
                created_at=datetime(2025, 1, 15, 10, 0),
                tweet_type="tweet",
            ),
            PostedTweet(
                text="Nice work!",
                created_at=datetime(2025, 1, 15, 11, 0),
                tweet_type="reply",
                original_author="ErikBjare",
            ),
        ],
    )
    result = format_workspace_activity_for_prompt(activity)
    assert "Social Media Activity" in result
    assert "Tweets posted**: 1" in result
    assert "Replies**: 1" in result
    assert "Hello world!" in result
    assert "@ErikBjare" in result


def test_format_workspace_activity_with_emails() -> None:
    """Formats email activity as markdown."""
    activity = WorkspaceActivity(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        emails=[
            SentEmail(
                subject="Project update",
                to="Erik <erik@example.com>",
                date=datetime(2025, 1, 15, 10, 0),
                body="Status update on the project.",
                in_reply_to="<original-msg>",
            ),
        ],
    )
    result = format_workspace_activity_for_prompt(activity)
    assert "Email Activity" in result
    assert "Emails sent**: 1" in result
    assert "Re: Project update" in result
    assert "Erik" in result
