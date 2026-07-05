"""
Read tweets and emails from workspace directories for summarization context.

Scans {workspace}/tweets/posted/*.yml and {workspace}/email/sent/*.md
to extract social interactions and communications for a date range.
"""

import email.utils
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PostedTweet:
    """A posted tweet or reply."""

    text: str
    created_at: datetime
    tweet_type: str = "tweet"  # "tweet" or "reply"
    in_reply_to: str = ""  # tweet ID replied to
    original_author: str = ""  # who we're replying to
    original_text: str = ""  # what we're replying to


@dataclass
class SentEmail:
    """A sent email."""

    subject: str
    to: str
    date: datetime
    body: str = ""
    in_reply_to: str = ""


@dataclass
class WorkspaceActivity:
    """Aggregated workspace activity (tweets + emails) for a date range."""

    start_date: date
    end_date: date
    tweets: list[PostedTweet] = field(default_factory=list)
    emails: list[SentEmail] = field(default_factory=list)


def _parse_yaml_simple(text: str) -> dict:
    """Simple YAML parser for tweet files (avoids pyyaml dependency).

    Only handles the flat top-level keys we need: created_at, text, type, in_reply_to.
    Falls back to empty dict on complex structures.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text) or {}
    except ImportError:
        pass

    # Minimal fallback: extract key: value pairs
    result: dict = {}
    for line in text.split("\n"):
        # Match simple key: value (not nested)
        match = re.match(r"^(\w+):\s+(.+)$", line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            # Strip quotes
            if (value.startswith("'") and value.endswith("'")) or (
                value.startswith('"') and value.endswith('"')
            ):
                value = value[1:-1]
            if value == "null":
                result[key] = None
            else:
                result[key] = value
    return result


def fetch_tweets(start: date, end: date, workspace: Path) -> list[PostedTweet]:
    """Fetch posted tweets/replies from workspace for a date range."""
    tweets_dir = workspace / "tweets" / "posted"
    if not tweets_dir.exists():
        logger.debug("Tweets directory not found: %s", tweets_dir)
        return []

    tweets: list[PostedTweet] = []

    for yml_path in sorted(tweets_dir.glob("*.yml")):
        # Quick date filter from filename: tweet_YYYYMMDD_*.yml or reply_YYYYMMDD_*.yml
        name_match = re.match(r"(?:tweet|reply)_(\d{8})", yml_path.name)
        if name_match:
            file_date_str = name_match.group(1)
            try:
                file_date = date(
                    int(file_date_str[:4]),
                    int(file_date_str[4:6]),
                    int(file_date_str[6:8]),
                )
                if file_date < start or file_date > end:
                    continue
            except ValueError:
                continue
        else:
            continue

        try:
            content = yml_path.read_text()
            data = _parse_yaml_simple(content)
        except OSError as e:
            logger.debug("Failed to read %s: %s", yml_path, e)
            continue

        text = data.get("text", "")
        if not text:
            continue

        # Parse created_at
        created_at_str = data.get("created_at", "")
        try:
            created_at = datetime.fromisoformat(str(created_at_str))
        except (ValueError, TypeError):
            continue

        tweet_type = data.get("type", "tweet")

        # For replies, try to extract original author from context
        original_author = ""
        original_text = ""
        context = data.get("context", {})
        if isinstance(context, dict):
            orig = context.get("original_tweet", {})
            if isinstance(orig, dict):
                original_author = orig.get("author", "")
                original_text = orig.get("text", "")

        tweets.append(
            PostedTweet(
                text=str(text),
                created_at=created_at,
                tweet_type=str(tweet_type),
                in_reply_to=str(data.get("in_reply_to", "") or ""),
                original_author=str(original_author),
                original_text=str(original_text),
            )
        )

    return tweets


def fetch_emails(start: date, end: date, workspace: Path) -> list[SentEmail]:
    """Fetch sent emails from workspace for a date range."""
    email_dir = workspace / "email" / "sent"
    if not email_dir.exists():
        logger.debug("Email directory not found: %s", email_dir)
        return []

    emails: list[SentEmail] = []

    for md_path in sorted(email_dir.glob("*.md")):
        try:
            content = md_path.read_text()
        except OSError as e:
            logger.debug("Failed to read %s: %s", md_path, e)
            continue

        # Parse MIME-style headers
        subject = ""
        to = ""
        date_str = ""
        in_reply_to = ""
        body = ""

        header_end = content.find("\n\n")
        if header_end == -1:
            continue

        headers = content[:header_end]
        body = content[header_end + 2 :].strip()

        for line in headers.split("\n"):
            if line.startswith("Subject: "):
                subject = line[9:].strip()
            elif line.startswith("To: "):
                to = line[4:].strip()
            elif line.startswith("Date: "):
                date_str = line[6:].strip()
            elif line.startswith("In-Reply-To: "):
                in_reply_to = line[13:].strip()

        if not date_str:
            continue

        # Parse date
        try:
            parsed = email.utils.parsedate_to_datetime(date_str)
            email_date = parsed.date()
        except (ValueError, TypeError):
            continue

        if email_date < start or email_date > end:
            continue

        # Strip HTML tags from body for plain text summary
        body_clean = re.sub(r"<[^>]+>", "", body).strip()

        emails.append(
            SentEmail(
                subject=subject,
                to=to,
                date=parsed,
                body=body_clean[:500],  # truncate for prompt context
                in_reply_to=in_reply_to,
            )
        )

    return emails


def fetch_workspace_activity(start: date, end: date, workspace: Path) -> WorkspaceActivity:
    """Fetch all workspace activity (tweets + emails) for a date range."""
    return WorkspaceActivity(
        start_date=start,
        end_date=end,
        tweets=fetch_tweets(start, end, workspace),
        emails=fetch_emails(start, end, workspace),
    )


def format_workspace_activity_for_prompt(activity: WorkspaceActivity) -> str:
    """Format workspace activity as markdown for prompt injection.

    Returns empty string if no activity data.
    """
    lines: list[str] = []

    if activity.tweets:
        lines.append("## Social Media Activity (Real Data)")
        lines.append(
            f"Period: {activity.start_date.isoformat()} to {activity.end_date.isoformat()}"
        )

        original_tweets = [t for t in activity.tweets if t.tweet_type == "tweet"]
        replies = [t for t in activity.tweets if t.tweet_type == "reply"]

        lines.append(f"- **Tweets posted**: {len(original_tweets)}, **Replies**: {len(replies)}")
        lines.append("")

        for t in original_tweets:
            # Truncate long tweets for context
            text = t.text[:200] + "..." if len(t.text) > 200 else t.text
            lines.append(f"- **Tweet**: {text}")

        for t in replies:
            text = t.text[:150] + "..." if len(t.text) > 150 else t.text
            if t.original_author:
                lines.append(f"- **Reply to @{t.original_author}**: {text}")
            else:
                lines.append(f"- **Reply**: {text}")

        lines.append("")

    if activity.emails:
        lines.append("## Email Activity (Real Data)")
        lines.append(
            f"Period: {activity.start_date.isoformat()} to {activity.end_date.isoformat()}"
        )
        lines.append(f"- **Emails sent**: {len(activity.emails)}")
        lines.append("")

        for e in activity.emails:
            # Extract just the name from "Name <email>" format
            to_name = e.to.split("<")[0].strip() if "<" in e.to else e.to
            is_reply = "Re: " if e.in_reply_to else ""
            lines.append(f"- **{is_reply}{e.subject}** (to {to_name})")

        lines.append("")

    return "\n".join(lines)
