#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10,<3.12"
# dependencies = [
#   "tweepy>=4.14.0",
#   "rich>=13.0.0",
#   "python-dotenv>=1.0.0",
#   "click>=8.0.0",
#   "pyyaml>=6.0.0",
#   "schedule>=1.2.0",
# ]
# [tool.uv]
# exclude-newer = "2024-01-01T00:00:00Z"
# ///
"""
Twitter Workflow Manager - Automated timeline monitoring and tweet drafting.

This script manages:
1. Reading timeline and drafting responses
2. Storing tweet drafts in a structured format
3. Review process for drafted tweets
4. Scheduled posting of approved tweets

Directory Structure:
tweets/
  new/       - New tweet drafts
  review/    - Tweets pending review
  approved/  - Approved tweets ready to post
  posted/    - Archive of posted tweets
  rejected/  - Rejected tweet drafts

Usage:
    ./twitter_workflow.py monitor              # Start timeline monitoring
    ./twitter_workflow.py draft "tweet text"   # Create new tweet draft
    ./twitter_workflow.py review               # Review pending tweets
    ./twitter_workflow.py post                 # Post approved tweets
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import click
import yaml
from rich.console import Console
from rich.prompt import Confirm, Prompt

from .llm import process_tweet, verify_draft
from .twitter import load_twitter_client

# Initialize rich console
console = Console()

# Default paths
TWEETS_DIR = Path("tweets")
NEW_DIR = TWEETS_DIR / "new"
REVIEW_DIR = TWEETS_DIR / "review"
APPROVED_DIR = TWEETS_DIR / "approved"
POSTED_DIR = TWEETS_DIR / "posted"
REJECTED_DIR = TWEETS_DIR / "rejected"

# Ensure directories exist
for dir in [NEW_DIR, REVIEW_DIR, APPROVED_DIR, POSTED_DIR, REJECTED_DIR]:
    dir.mkdir(parents=True, exist_ok=True)


class TweetDraft:
    """Represents a tweet draft with metadata"""

    def __init__(
        self,
        text: str,
        type: str = "tweet",
        in_reply_to: Optional[str] = None,
        scheduled_time: Optional[datetime] = None,
        context: Optional[Dict] = None,
    ):
        self.text = text
        self.type = type  # tweet, reply, thread
        self.in_reply_to = in_reply_to
        self.scheduled_time = scheduled_time
        self.context = context or {}
        self.created_at = datetime.now()

    def to_dict(self) -> Dict:
        """Convert to dictionary for storage"""
        return {
            "text": self.text,
            "type": self.type,
            "in_reply_to": self.in_reply_to,
            "scheduled_time": (self.scheduled_time.isoformat() if self.scheduled_time else None),
            "context": self.context,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "TweetDraft":
        """Create from dictionary"""
        draft = cls(
            text=data["text"],
            type=data["type"],
            in_reply_to=data["in_reply_to"],
            scheduled_time=(datetime.fromisoformat(data["scheduled_time"]) if data["scheduled_time"] else None),
            context=data["context"],
        )
        draft.created_at = datetime.fromisoformat(data["created_at"])
        return draft

    def save(self, path: Path) -> None:
        """Save draft to file"""
        with path.open("w") as f:
            yaml.dump(self.to_dict(), f)

    @classmethod
    def load(cls, path: Path) -> "TweetDraft":
        """Load draft from file"""
        with path.open("r") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)


def generate_draft_name(draft: TweetDraft) -> str:
    """Generate a filename for a tweet draft"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{draft.type}_{timestamp}"
    if draft.in_reply_to:
        prefix += f"_reply_{draft.in_reply_to}"
    return f"{prefix}.yml"


def save_draft(draft: TweetDraft, status: str = "new") -> Path:
    """Save a tweet draft to the appropriate directory"""
    status_dirs = {
        "new": NEW_DIR,
        "review": REVIEW_DIR,
        "approved": APPROVED_DIR,
        "posted": POSTED_DIR,
        "rejected": REJECTED_DIR,
    }

    if status not in status_dirs:
        raise ValueError(f"Invalid status: {status}")

    path = status_dirs[status] / generate_draft_name(draft)
    draft.save(path)
    return path


def move_draft(path: Path, new_status: str) -> Path:
    """Move a draft to a new status directory"""
    draft = TweetDraft.load(path)
    new_path = save_draft(draft, new_status)
    path.unlink()
    return new_path


def list_drafts(status: str) -> List[Path]:
    """List all drafts in a status directory"""
    status_dirs = {
        "new": NEW_DIR,
        "review": REVIEW_DIR,
        "approved": APPROVED_DIR,
        "posted": POSTED_DIR,
        "rejected": REJECTED_DIR,
    }

    if status not in status_dirs:
        raise ValueError(f"Invalid status: {status}")

    return sorted(status_dirs[status].glob("*.yml"))


@click.group()
def cli():
    """Twitter Workflow Manager"""
    pass


@cli.command()
@click.argument("text")
@click.option("--type", default="tweet", type=click.Choice(["tweet", "reply", "thread"]))
@click.option("--reply-to", help="Tweet ID to reply to")
@click.option("--schedule", help="Schedule time (ISO format)")
def draft(text: str, type: str, reply_to: Optional[str], schedule: Optional[str]) -> None:
    """Create a new tweet draft"""
    scheduled_time = datetime.fromisoformat(schedule) if schedule else None

    draft = TweetDraft(
        text=text,
        type=type,
        in_reply_to=reply_to,
        scheduled_time=scheduled_time,
    )

    path = save_draft(draft, "new")
    console.print(f"[green]Created draft: {path}")


@cli.command()
@click.option(
    "--auto-approve",
    is_flag=True,
    help="Automatically approve drafts that pass all checks",
)
@click.option("--show-context", is_flag=True, help="Show full context for each draft")
def review(auto_approve: bool, show_context: bool) -> None:
    """Review pending tweet drafts with LLM assistance"""

    drafts = list_drafts("new")

    if not drafts:
        console.print("[yellow]No drafts to review")
        return

    for path in drafts:
        draft = TweetDraft.load(path)

        console.print("\n[bold]Reviewing draft:[/bold]")
        console.print(f"[cyan]Type: {draft.type}")
        if draft.in_reply_to:
            console.print(f"[cyan]Reply to: {draft.in_reply_to}")
        if draft.scheduled_time:
            console.print(f"[cyan]Scheduled: {draft.scheduled_time}")
        console.print(f"[white]{draft.text}")

        # Show context if requested
        if show_context and draft.context:
            console.print("\n[bold]Context:[/bold]")
            if "original_tweet" in draft.context:
                orig = draft.context["original_tweet"]
                console.print(f"[blue]Original tweet by @{orig['author']}:[/blue]")
                console.print(f"[white]{orig['text']}[/white]")
            if "evaluation" in draft.context:
                eval_data = draft.context["evaluation"]
                console.print("[blue]Evaluation:[/blue]")
                console.print(f"Relevance: {eval_data['relevance']}/100")
                console.print(f"Priority: {eval_data['priority']}/100")
                console.print(f"Reasoning: {eval_data['reasoning']}")

        # Get LLM review
        approved, review_result = verify_draft(draft.to_dict())

        # Display review results
        console.print("\n[bold]Review Results:[/bold]")
        for criterion, result in review_result.criteria_results.items():
            status = "✅" if result.pass_ else "❌"
            console.print(f"{status} {criterion}: {result.notes}")

        if review_result.improvements:
            console.print("\n[yellow]Suggested improvements:[/yellow]")
            for improvement in review_result.improvements:
                console.print(f"• {improvement}")

        # Handle auto-approve or ask for action
        if auto_approve and approved:
            move_draft(path, "approved")
            console.print("[green]Draft automatically approved")
        else:
            action = Prompt.ask("Action", choices=["approve", "reject", "skip", "edit"], default="skip")

            if action == "approve":
                move_draft(path, "approved")
                console.print("[green]Draft approved")
            elif action == "reject":
                move_draft(path, "rejected")
                console.print("[red]Draft rejected")
            elif action == "edit":
                # Allow editing the draft
                new_text = Prompt.ask("Edit tweet", default=draft.text)
                if new_text != draft.text:
                    draft.text = new_text
                    draft.save(path)
                    console.print("[blue]Draft updated, will need another review")
            else:
                console.print("[yellow]Draft skipped")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Don't actually post tweets")
def post(dry_run: bool) -> None:
    """Post approved tweets"""

    drafts = list_drafts("approved")
    if not drafts:
        console.print("[yellow]No approved tweets to post")
        return

    client = load_twitter_client(require_auth=True)

    for path in drafts:
        draft = TweetDraft.load(path)

        # Skip if scheduled for later
        if draft.scheduled_time and draft.scheduled_time > datetime.now():
            continue

        console.print("\n[bold]Posting tweet:[/bold]")
        console.print(f"[cyan]Type: {draft.type}")
        if draft.in_reply_to:
            console.print(f"[cyan]Reply to: {draft.in_reply_to}")
        console.print(f"[white]{draft.text}")

        if not dry_run and Confirm.ask("Post this tweet?", default=True):
            try:
                response = client.create_tweet(
                    text=draft.text,
                    in_reply_to_tweet_id=draft.in_reply_to,
                    user_auth=False,
                )

                if response.data:
                    tweet_id = response.data["id"]
                    console.print(f"[green]Posted tweet: {tweet_id}")
                    move_draft(path, "posted")
                else:
                    console.print("[red]Error: No response data from tweet creation")
            except Exception as e:
                console.print(f"[red]Error posting tweet: {e}")
        else:
            console.print("[yellow]Skipped posting tweet")


def process_timeline_tweets(
    tweets,
    users,
    source: str,
    client,
    times: Optional[int] = None,
    dry_run: bool = False,
    max_drafts: Optional[int] = None,
):
    """Process tweets from timeline"""
    drafts_generated = 0

    # Create lookup for user info
    user_lookup = {user.id: user for user in users} if users else {}
    tweets_processed = 0

    console.print(f"\n[bold]Processing tweets from {source}[/bold]")
    if times:
        console.print(f"[blue]Will process {times} tweet(s)[/blue]")

    for tweet in tweets:
        tweets_processed += 1
        try:
            # Skip our own tweets
            if tweet.author_id == client.get_me(user_auth=False).data.id:
                continue

            # Get author info
            author = user_lookup.get(tweet.author_id)
            author_username = author.username if author else str(tweet.author_id)

            # Prepare tweet data for processing
            tweet_data = {
                "id": tweet.id,
                "text": tweet.text,
                "author": author_username,
                "created_at": tweet.created_at.isoformat(),
                "context": {
                    "public_metrics": tweet.public_metrics,
                    "author_metrics": (
                        {
                            "followers": (author.public_metrics["followers_count"] if author else 0),
                        }
                        if author
                        else {}
                    ),
                },
            }

            # Process tweet
            console.print(f"\n[cyan]Processing tweet from @{author_username}:[/cyan]")
            console.print(f"[white]{tweet.text}[/white]")

            eval_result, response = process_tweet(tweet_data)

            # Show evaluation result
            console.print(f"[blue]Evaluation: {eval_result.action.upper()}[/blue]")
            console.print(f"[blue]Relevance: {eval_result.relevance}/100[/blue]")
            console.print(f"[blue]Priority: {eval_result.priority}/100[/blue]")

            if response:
                # Create draft from response
                draft = TweetDraft(
                    text=response.text,
                    type=response.type,
                    in_reply_to=tweet.id if response.type == "reply" else None,
                    context={
                        "original_tweet": tweet_data,
                        "evaluation": eval_result,
                        "response_metadata": response,
                    },
                )

                if not dry_run:
                    # Save draft
                    path = save_draft(draft, "new")
                    console.print(f"[green]Created draft response: {path}")
                else:
                    console.print("[yellow]Would create draft response:")
                    console.print(f"[white]{draft.text}")

                # If thread needed, create follow-up drafts
                if response.thread_needed and response.follow_up:
                    for i, follow_up in enumerate([response.follow_up], 1):
                        thread_draft = TweetDraft(
                            text=follow_up,
                            type="thread",
                            context={
                                "thread_position": i,
                                "original_tweet": tweet_data,
                                "evaluation": eval_result,
                            },
                        )
                        if not dry_run:
                            thread_path = save_draft(thread_draft, "new")
                            console.print(f"[green]Created thread draft {i}: {thread_path}")
                            drafts_generated += 1
                        else:
                            console.print(f"[yellow]Would create thread draft {i}:")
                            console.print(f"[white]{thread_draft.text}")

                        if not dry_run and max_drafts and drafts_generated >= max_drafts:
                            console.print("[yellow]Reached maximum number of drafts, stopping...")
                            return

            if times and tweets_processed >= times:
                console.print(f"[yellow]Processed {times} tweet(s), stopping...")
                return

        except Exception as e:
            logging.exception(f"Error processing tweet {tweet.id}")
            console.print(f"[red]Error processing tweet {tweet.id}: {e}")
            raise SystemExit(1)


@cli.command()
@click.option("--list-id", help="Twitter list ID to monitor")
@click.option("--interval", default=300, help="Check interval in seconds")
@click.option("--dry-run", is_flag=True, help="Process tweets but don't save drafts")
@click.option(
    "-n",
    "--times",
    type=int,
    help="Number of tweets to process before exiting",
)
def monitor(list_id: Optional[str], interval: int, dry_run: bool, times: Optional[int]) -> None:
    """Monitor timeline and generate draft replies"""

    console.print("[green]Starting timeline monitor...")
    console.print(f"[blue]Checking every {interval} seconds")

    # Initialize Twitter client
    client = load_twitter_client(require_auth=True)

    def check_timeline():
        """Check timeline and generate drafts"""
        try:
            # Get timeline tweets
            if list_id:
                tweets = client.get_list_tweets(
                    list_id,
                    tweet_fields=["created_at", "author_id", "public_metrics"],
                    expansions=["author_id"],
                    user_fields=["username", "public_metrics"],
                    user_auth=False,
                )
                source = f"list {list_id}"
            else:
                tweets = client.get_home_timeline(
                    tweet_fields=["created_at", "author_id", "public_metrics"],
                    expansions=["author_id"],
                    user_fields=["username", "public_metrics"],
                    user_auth=False,
                )
                source = "home timeline"

            if not tweets.data:
                console.print("[yellow]No new tweets found")
                return

            # Process tweets
            if dry_run:
                console.print("[yellow]DRY RUN: Processing tweets but not saving drafts")

            process_timeline_tweets(
                tweets.data,
                tweets.includes["users"] if tweets.includes else None,
                source,
                client,
                times=times,
                dry_run=dry_run,
                max_drafts=times,  # Use times parameter as max_drafts
            )

        except Exception as e:
            console.print(f"[red]Error checking timeline: {e}")
            console.print(f"[red]Details: {str(e)}")

    # Run once if times is set, otherwise loop
    check_timeline()

    if not times:
        try:
            while True:
                time.sleep(interval)
                check_timeline()
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopping timeline monitor...")


if __name__ == "__main__":
    cli()
