#!/usr/bin/env python3
# Twitter Workflow Manager using dedicated venv
# Dependencies installed in .venv: tweepy, rich, python-dotenv, click, pyyaml, flask, gptme
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
    ./workflow.py monitor              # Start timeline monitoring
    ./workflow.py draft "tweet text"   # Create new tweet draft
    ./workflow.py review               # Review pending tweets
    ./workflow.py post                 # Post approved tweets
"""

# Import stdlib email.message BEFORE sys.path manipulation
# This ensures the stdlib module is cached and not shadowed by any local directories
import email.message  # noqa: F401

import sys
from pathlib import Path as _Path

# Add current directory to path FIRST so local twitter/ is found
# This prevents conflict with any installed 'twitter' package
sys.path.insert(0, str(_Path(__file__).parent))
# Add parent directory for shared communication_utils
sys.path.insert(0, str(_Path(__file__).parent.parent))

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)

import click
import yaml
from gptme.init import init as init_gptme
from rich.console import Console
from rich.prompt import Confirm, Prompt

from twitter.llm import (
    EvaluationResponse,
    TweetResponse,
    process_tweet,
    verify_draft,
)
from twitter.twitter import cached_get_me, load_twitter_client
from trusted_users import is_trusted_user  # type: ignore

# Import monitoring utilities
from communication_utils.monitoring import get_logger, MetricsCollector

logger = get_logger(__name__, "twitter")
metrics = MetricsCollector()

# Initialize rich console
console = Console()


# Calculate agent directory (3 parents up from this script)
# Script is at: agent_dir/gptme-contrib/scripts/twitter/workflow.py
def _find_agent_dir() -> Path:
    """
    Find agent directory by locating git repository structure.

    Returns the parent directory of the git repository this script is in,
    which should be the agent workspace when gptme-contrib is a submodule.

    Falls back to Path(__file__).parent.parent.parent.parent if git detection fails.
    """
    import subprocess

    try:
        # Find root of current git repo (gptme-contrib)
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            check=True,
        )
        contrib_root = Path(result.stdout.strip())

        # Parent directory should be agent workspace
        agent_dir = contrib_root.parent

        # Verify parent is also a git repo (agent workspace)
        parent_check = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=agent_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        parent_root = Path(parent_check.stdout.strip())

        # If parent git root matches parent_dir, we found agent workspace
        if parent_root == agent_dir:
            return agent_dir

    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Fallback to old method if git detection fails
    return Path(__file__).parent.parent.parent.parent


AGENT_DIR = _find_agent_dir()

# Default paths (agent-dir-relative)
TWEETS_DIR = AGENT_DIR / "tweets"
NEW_DIR = TWEETS_DIR / "new"
REVIEW_DIR = TWEETS_DIR / "review"
APPROVED_DIR = TWEETS_DIR / "approved"
POSTED_DIR = TWEETS_DIR / "posted"
REJECTED_DIR = TWEETS_DIR / "rejected"
CACHE_DIR = TWEETS_DIR / "cache"

# Ensure directories exist
for dir in [NEW_DIR, REVIEW_DIR, APPROVED_DIR, POSTED_DIR, REJECTED_DIR, CACHE_DIR]:
    dir.mkdir(parents=True, exist_ok=True)


def get_cache_path(tweet_id: str) -> Path:
    """Get the cache file path for a tweet ID"""
    return CACHE_DIR / f"{tweet_id}.json"


def is_tweet_cached(tweet_id: str) -> bool:
    """Check if a tweet has been processed and cached"""
    cache_path = get_cache_path(tweet_id)
    return cache_path.exists()


def save_to_cache(tweet_id: str, eval_result, response=None) -> None:
    """Save tweet processing results to cache"""
    cache_path = get_cache_path(tweet_id)
    cache_data = {
        "tweet_id": tweet_id,
        "processed_at": datetime.now().isoformat(),
        "evaluation": asdict(eval_result) if eval_result else None,
        "response": asdict(response) if response else None,
    }

    with open(cache_path, "w") as f:
        json.dump(cache_data, f, indent=2)

    console.print(f"[blue]Cached results for tweet {tweet_id}")


def load_from_cache(tweet_id: str) -> Tuple[Optional[dict], Optional[dict]]:
    """Load tweet processing results from cache"""
    cache_path = get_cache_path(tweet_id)

    if not cache_path.exists():
        return None, None

    with open(cache_path, "r") as f:
        cache_data = json.load(f)

    return cache_data.get("evaluation"), cache_data.get("response")


class TweetDraft:
    """Represents a tweet draft with metadata"""

    def __init__(
        self,
        text: str,
        type: str = "tweet",
        in_reply_to: Optional[str] = None,
        scheduled_time: Optional[datetime] = None,
        context: Optional[Dict] = None,
        reject_reason: Optional[str] = None,
        quality_score: Optional[int] = None,
        thread: Optional[List[str]] = None,
    ):
        self.text = text
        self.type = type  # tweet, reply, thread
        self.in_reply_to = in_reply_to
        self.scheduled_time = scheduled_time
        self.context = context or {}
        self.created_at = datetime.now()
        self.reject_reason = reject_reason
        self.quality_score = quality_score  # 0-3 star rating (matches blog post system)
        self.thread = thread or []  # List of follow-up tweet texts

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage"""
        result: Dict[str, Any] = {
            "text": self.text,
            "type": self.type,
            "in_reply_to": self.in_reply_to,
            "scheduled_time": (
                self.scheduled_time.isoformat() if self.scheduled_time else None
            ),
            "context": self.context,
            "created_at": self.created_at.isoformat(),
        }
        # Only include reject_reason if it's set
        if self.reject_reason:
            result["reject_reason"] = self.reject_reason
        # Only include quality_score if it's set
        if self.quality_score is not None:
            result["quality_score"] = self.quality_score
        # Only include thread if it's set and non-empty
        if self.thread:
            result["thread"] = self.thread
        return result

    @classmethod
    def from_dict(cls, data: Dict) -> "TweetDraft":
        """Create from dictionary"""
        draft = cls(
            text=data["text"],
            type=data["type"],
            in_reply_to=data.get("in_reply_to"),
            scheduled_time=(
                data["scheduled_time"]
                if isinstance(data.get("scheduled_time"), datetime)
                else (
                    datetime.fromisoformat(data["scheduled_time"])
                    if data.get("scheduled_time")
                    else None
                )
            ),
            context=data["context"],
            reject_reason=data.get("reject_reason"),  # Optional, backward compatible
            quality_score=data.get("quality_score"),  # Optional, backward compatible
            thread=data.get("thread"),  # Optional, backward compatible
        )
        created_at = data["created_at"]
        if isinstance(created_at, str):
            draft.created_at = datetime.fromisoformat(created_at)
        elif isinstance(created_at, datetime):
            draft.created_at = created_at
        else:
            raise ValueError(f"Invalid created_at format: {type(created_at)}")
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


def compute_quality_score(review_result) -> int:
    """
    Compute quality score (0-3 stars) from review result.

    Scoring:
    - 3 stars: All criteria passed + approval recommendation
    - 2 stars: Most criteria passed (≥75%)
    - 1 star: Some criteria passed (≥50%)
    - 0 stars: Less than half passed
    """
    if not review_result.criteria_results:
        return 0

    total = len(review_result.criteria_results)
    passed = sum(1 for r in review_result.criteria_results.values() if r.passed)
    pass_rate = passed / total if total > 0 else 0

    # All passed + approval recommendation = 3 stars
    if pass_rate == 1.0 and review_result.recommendation == "approve":
        return 3
    # Most passed (≥75%) = 2 stars
    elif pass_rate >= 0.75:
        return 2
    # Some passed (≥50%) = 1 star
    elif pass_rate >= 0.50:
        return 1
    # Less than half passed = 0 stars
    else:
        return 0


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


def get_conversation_thread(
    client, tweet_id_or_conversation_id, max_pages=3, max_tweets_per_page=100
):
    """
    Retrieve the complete conversation thread for a given tweet ID or conversation ID.

    Args:
        client: The Twitter API client
        tweet_id_or_conversation_id: Either a tweet ID or a conversation ID
        max_pages: Maximum number of pagination pages to fetch (default: 3)
        max_tweets_per_page: Maximum number of tweets per page (default: 100)

    Returns:
        List of tweet data dictionaries in chronological order with thread structure information
    """
    try:
        # Step 1: Determine if we have a tweet ID or conversation ID
        conversation_id = tweet_id_or_conversation_id

        # Ensure tweet_id_or_conversation_id is a string before checking
        tweet_id_str = str(tweet_id_or_conversation_id)

        # If this looks like a tweet ID, get the conversation ID first
        if not tweet_id_str.startswith("conversation_"):
            # Get the original tweet with comprehensive metadata
            try:
                tweet_response = client.get_tweet(
                    tweet_id_or_conversation_id,
                    tweet_fields=[
                        "created_at",
                        "author_id",
                        "public_metrics",
                        "conversation_id",
                        "in_reply_to_user_id",
                        "referenced_tweets",
                    ],
                    expansions=[
                        "author_id",
                        "referenced_tweets.id",
                        "in_reply_to_user_id",
                    ],
                    user_fields=["username", "name"],
                )

                if tweet_response.data and tweet_response.data.conversation_id:
                    conversation_id = tweet_response.data.conversation_id

                    # Initialize collections with the original tweet
                    all_tweets = {tweet_response.data.id: tweet_response.data}
                    all_users = {}

                    # Add users from the original tweet response
                    if tweet_response.includes and "users" in tweet_response.includes:
                        all_users.update(
                            {user.id: user for user in tweet_response.includes["users"]}
                        )
                else:
                    # If we couldn't get the tweet, just use the ID as conversation ID
                    all_tweets = {}
                    all_users = {}
            except Exception as e:
                console.print(f"[yellow]Error fetching original tweet: {e}")
                all_tweets = {}
                all_users = {}
        else:
            # Initialize empty collections if starting with conversation ID
            all_tweets = {}
            all_users = {}

        # Step 2: Get all tweets in the conversation with pagination
        next_token = None
        page_count = 0

        while page_count < max_pages:
            # Prepare pagination parameters
            kwargs = {
                "query": f"conversation_id:{conversation_id}",
                "max_results": max_tweets_per_page,
                "tweet_fields": [
                    "created_at",
                    "author_id",
                    "public_metrics",
                    "in_reply_to_user_id",
                    "referenced_tweets",
                ],
                "expansions": [
                    "author_id",
                    "referenced_tweets.id",
                    "in_reply_to_user_id",
                ],
                "user_fields": ["username", "name"],
                "user_auth": False,
            }

            # Add pagination token if available
            if next_token:
                kwargs["next_token"] = next_token

            # Execute the search
            conversation = client.search_recent_tweets(**kwargs)
            page_count += 1

            # Process results
            if conversation.data:
                # Add tweets to our collection, avoiding duplicates
                for reply in conversation.data:
                    all_tweets[reply.id] = reply

                # Add users to our collection
                if conversation.includes and "users" in conversation.includes:
                    all_users.update(
                        {user.id: user for user in conversation.includes["users"]}
                    )
            else:
                # No data in this page
                break

            # Check if there are more pages
            if (
                not hasattr(conversation, "meta")
                or "next_token" not in conversation.meta
            ):
                break

            next_token = conversation.meta["next_token"]

        # Step 3: No tweets found
        if not all_tweets:
            return []

        # Step 4: Build the thread structure
        # Create a mapping of tweets by ID for easy lookup
        sorted_tweets = sorted(all_tweets.values(), key=lambda t: t.created_at)

        # Create a reply structure mapping
        reply_structure = {}
        for t in sorted_tweets:
            if hasattr(t, "referenced_tweets") and t.referenced_tweets:
                for ref in t.referenced_tweets:
                    if ref.type == "replied_to":
                        reply_structure[t.id] = ref.id
                        break

        # Step 5: Build the final thread context
        thread_context = []
        for t in sorted_tweets:
            # Get author username
            author = all_users.get(t.author_id)
            author_username = author.username if author else str(t.author_id)

            # Calculate depth in thread
            depth = 0
            current_id = t.id
            while current_id in reply_structure:
                depth += 1
                current_id = reply_structure[current_id]

            # Create thread context entry
            thread_entry = {
                "id": t.id,
                "text": t.text,
                "author": author_username,
                "created_at": t.created_at.isoformat(),
                "depth": depth,  # Add depth information for UI indentation
                "replied_to_id": reply_structure.get(
                    t.id
                ),  # Which tweet this is replying to
                "public_metrics": (
                    t.public_metrics if hasattr(t, "public_metrics") else {}
                ),
                # Include referenced tweets if available
                "referenced_tweets": [],
            }

            # Add referenced tweets information
            if hasattr(t, "referenced_tweets") and t.referenced_tweets:
                for ref in t.referenced_tweets:
                    ref_tweet = None
                    for tweet in sorted_tweets:
                        if tweet.id == ref.id:
                            ref_tweet = tweet
                            break
                    else:
                        # Tweet not found in the conversation
                        logging.warning(
                            f"Referenced tweet {ref.id} not found in conversation"
                        )
                        continue

                    author = all_users.get(ref_tweet.author_id)
                    assert author, f"Author not found for referenced tweet {ref.id}"
                    thread_entry["referenced_tweets"].append(
                        {
                            "type": ref.type,
                            "id": ref.id,
                            "text": ref_tweet.text if ref_tweet else "Unavailable",
                            "author": (
                                author.username
                                if ref_tweet and ref_tweet.author_id in all_users
                                else "Unknown"
                            ),
                        }
                    )

            thread_context.append(thread_entry)

        # Log success
        console.print(
            f"[blue]Retrieved {len(thread_context)} tweets from conversation {conversation_id}"
        )

        return thread_context

    except Exception as e:
        console.print(f"[yellow]Error retrieving conversation thread: {e}")
        return []


def move_draft(path: Path, new_status: str) -> Path:
    """Move a draft to a new status directory"""
    # Get the appropriate status directory
    status_dirs = {
        "new": NEW_DIR,
        "review": REVIEW_DIR,
        "approved": APPROVED_DIR,
        "posted": POSTED_DIR,
        "rejected": REJECTED_DIR,
    }

    if new_status not in status_dirs:
        raise ValueError(f"Invalid status: {new_status}")

    # Simply move the file to the new directory, preserving the filename
    new_path = status_dirs[new_status] / path.name

    # Load and save to ensure any updates are persisted
    draft = TweetDraft.load(path)
    draft.save(new_path)

    # Remove the original file
    path.unlink()

    return new_path


def find_draft(
    draft_id: str, status: Optional[str] = None, show_error: bool = True
) -> Optional[Path]:
    """Find a draft by ID or path.

    Args:
        draft_id: Either a simple ID, filename, or full path
        status: Optional status to look in specific directory
               If None, looks in all status directories
        show_error: Whether to print error message if not found

    Returns:
        Path to draft if found, None otherwise
    """
    status_dirs = {
        "new": NEW_DIR,
        "review": REVIEW_DIR,
        "approved": APPROVED_DIR,
        "posted": POSTED_DIR,
        "rejected": REJECTED_DIR,
    }

    # Handle full paths
    if "/" in draft_id:
        draft_path = Path(draft_id)
        if not draft_path.exists() and not draft_path.suffix:
            draft_path = draft_path.with_suffix(".yml")
        if draft_path.exists():
            return draft_path
    else:
        # Search in specified directories
        search_dirs = [status_dirs[status]] if status else status_dirs.values()
        for dir in search_dirs:
            for path in [dir / draft_id, (dir / draft_id).with_suffix(".yml")] + list(
                dir.glob(f"*{draft_id}*.yml")
            ):
                if path.exists():
                    return path

    if show_error:
        status_msg = f" in {status} directory" if status else ""
        console.print(f"[red]No draft found{status_msg}: {draft_id}")
        console.print(
            "[yellow]ID can be: simple ID, filename, or full path (e.g., tweet_20250419, reply_*.yml, tweets/new/*.yml)"
        )

    return None


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
@click.option(
    "--model",
    default=os.getenv("MODEL", "anthropic/claude-sonnet-4-5"),
    help="Model to use for LLM operations",
)
def cli(model: str | None = None) -> None:
    """Twitter Workflow Manager"""
    init_gptme(
        model=model, interactive=False, tool_allowlist=[], tool_format="markdown"
    )


@cli.command()
@click.argument("text")
@click.option(
    "--type", default="tweet", type=click.Choice(["tweet", "reply", "thread"])
)
@click.option("--reply-to", help="Tweet ID to reply to")
@click.option("--schedule", help="Schedule time (ISO format)")
def draft(
    text: str, type: str, reply_to: Optional[str], schedule: Optional[str]
) -> None:
    """Create a new tweet draft"""
    op = metrics.start_operation("draft_creation", "twitter")

    try:
        scheduled_time = datetime.fromisoformat(schedule) if schedule else None

        draft = TweetDraft(
            text=text,
            type=type,
            in_reply_to=reply_to,
            scheduled_time=scheduled_time,
        )

        path = save_draft(draft, "new")

        op.complete(success=True)
        logger.info(
            "Draft created successfully",
            extra={
                "type": type,
                "scheduled": bool(scheduled_time),
                "text_length": len(text),
                "path": str(path),
            },
        )

        console.print(f"[green]Created draft: {path}")
    except Exception as e:
        op.complete(success=False, error=str(e))
        logger.error("Draft creation failed", extra={"type": type, "error": str(e)})
        raise


def _check_for_duplicate_replies_internal(draft: TweetDraft) -> dict[str, list[Path]]:
    """Internal utility to check if we already have replies to the same tweet.

    Returns dict with keys: 'posted', 'approved', 'new' containing paths to duplicates.
    """
    if not draft.in_reply_to:
        return {}

    duplicates = {}
    tweet_id = draft.in_reply_to

    # Check each directory
    for status in ["posted", "approved", "new"]:
        status_dir = TWEETS_DIR / status
        if not status_dir.exists():
            continue

        matching_drafts = []
        for draft_path in status_dir.glob("*.yml"):
            try:
                other_draft = TweetDraft.load(draft_path)
                if other_draft.in_reply_to == tweet_id:
                    matching_drafts.append(draft_path)
            except Exception:
                continue

        if matching_drafts:
            duplicates[status] = matching_drafts

    return duplicates


@cli.command()
@click.argument("draft_path", type=click.Path(exists=True))
def check_for_duplicate_replies(draft_path: str):
    """Check if we already have replies to the same tweet (CLI command).

    DRAFT_PATH: Path to the draft file to check for duplicates.
    """
    draft = TweetDraft.load(Path(draft_path))
    duplicates = _check_for_duplicate_replies_internal(draft)

    if not duplicates:
        click.echo("No duplicate replies found.")
        return

    click.echo(f"Found duplicate replies to tweet {draft.in_reply_to}:")
    for status, paths in duplicates.items():
        click.echo(f"\n{status.upper()}:")
        for path in paths:
            click.echo(f"  - {path}")


def group_drafts_by_reply_target(drafts: list[Path]) -> dict[str | None, list[Path]]:
    """Group drafts by the tweet they're replying to.

    Returns dict: tweet_id -> [draft_paths]
    None key for non-reply tweets.
    """
    groups: dict[str | None, list[Path]] = {}

    for draft_path in drafts:
        try:
            draft = TweetDraft.load(draft_path)
            target = draft.in_reply_to

            if target not in groups:
                groups[target] = []
            groups[target].append(draft_path)
        except Exception:
            continue

    return groups


@cli.command()
@click.option("--auto-approve", is_flag=True, help="Automatically approve good drafts")
@click.option("--show-context", is_flag=True, help="Show full context for each draft")
@click.option(
    "--dry-run", is_flag=True, help="Don't actually approve/reject, just show analysis"
)
def review(auto_approve: bool, show_context: bool, dry_run: bool) -> None:
    """Review pending tweet drafts with LLM assistance"""

    drafts = list_drafts("new")

    if not drafts:
        console.print("[yellow]No drafts to review")
        return

    # Group drafts by reply target to handle potential duplicates
    grouped_drafts = group_drafts_by_reply_target(drafts)

    # Process each group
    for reply_target, group_paths in grouped_drafts.items():
        # If multiple drafts reply to same tweet, show warning
        if len(group_paths) > 1:
            console.print(
                f"\n[yellow]⚠ Warning: {len(group_paths)} drafts reply to same tweet!"
            )
            console.print(f"[yellow]Tweet ID: {reply_target}")
            console.print("[yellow]You can choose the best option or reject duplicates")

        for path in group_paths:
            draft = TweetDraft.load(path)

            console.print("\n[bold]Reviewing draft:[/bold]")
            console.print(f"[cyan]Type: {draft.type}")
            if draft.in_reply_to:
                console.print(f"[cyan]Reply to: {draft.in_reply_to}")

                # Check for duplicate replies
                duplicates = _check_for_duplicate_replies_internal(draft)
                if duplicates:
                    console.print("\n[yellow]⚠ Duplicate Detection:[/yellow]")
                    if "posted" in duplicates:
                        console.print(
                            f"[red]Already posted {len(duplicates['posted'])} reply(ies) to this tweet"
                        )
                        for dup_path in duplicates["posted"]:
                            dup_draft = TweetDraft.load(dup_path)
                            console.print(f"[red]  - Posted: {dup_draft.text[:80]}...")
                    if "approved" in duplicates:
                        console.print(
                            f"[yellow]Found {len(duplicates['approved'])} approved draft(s) for this tweet"
                        )
                        for dup_path in duplicates["approved"]:
                            dup_draft = TweetDraft.load(dup_path)
                            console.print(
                                f"[yellow]  - Approved: {dup_draft.text[:80]}..."
                            )
                    console.print(
                        "[yellow]Consider rejecting this duplicate or editing to make it distinct\n"
                    )
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

        # Compute and save quality score
        quality_score = compute_quality_score(review_result)
        draft.quality_score = quality_score
        draft.save(path)  # Persist quality_score to file

        # Display review results
        stars = "★" * quality_score + "☆" * (3 - quality_score)
        console.print(f"\n[bold]Quality Score: {stars} ({quality_score}/3)[/bold]")
        console.print("\n[bold]Review Results:[/bold]")
        for criterion, result in review_result.criteria_results.items():
            status = "✅" if result.passed else "❌"
            console.print(f"{status} {criterion}: {result.notes}")

        if review_result.improvements:
            console.print("\n[yellow]Suggested improvements:[/yellow]")
            for improvement in review_result.improvements:
                console.print(f"• {improvement}")

        # Handle auto-approve or ask for action
        if auto_approve and approved:
            move_draft(path, "approved")
            console.print("[green]Draft automatically approved")
        elif dry_run:
            # Just show review results, don't prompt for action
            console.print("[yellow]Dry run - no action taken")
            console.print(f"[blue]Draft ID: {path.stem}[/blue]")
            console.print(
                "[blue]Use 'workflow approve/reject/edit <id>' to process this draft[/blue]"
            )
        else:
            action = Prompt.ask(
                "Action", choices=["approve", "reject", "skip", "edit"], default="skip"
            )

            if action == "approve":
                move_draft(path, "approved")
                console.print("[green]Draft approved")
            elif action == "reject":
                # Prompt for rejection reason
                reason = Prompt.ask(
                    "Rejection reason (optional)",
                    default="",
                )
                if reason:
                    draft.reject_reason = reason
                    draft.save(path)  # Save with reject_reason before moving
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
@click.argument("draft_id")
def approve(draft_id: str) -> None:
    """Approve a draft tweet by ID or path"""
    draft_path = find_draft(draft_id, "new")
    if not draft_path:
        return

    new_path = move_draft(draft_path, "approved")
    console.print(f"[green]Draft approved: {draft_path.name} → {new_path.name}")


@cli.command()
@click.argument("draft_id")
@click.option("--reason", "-r", help="Reason for rejection", default=None)
def reject(draft_id: str, reason: Optional[str]) -> None:
    """Reject a draft tweet by ID (works on both new and approved drafts)"""
    # Try to find draft in either new or approved directories
    draft_path = find_draft(draft_id, "new", show_error=False) or find_draft(
        draft_id, "approved"
    )
    if not draft_path:
        return

    # Add rejection reason if provided
    if reason:
        draft = TweetDraft.load(draft_path)
        draft.reject_reason = reason
        draft.save(draft_path)

    new_path = move_draft(draft_path, "rejected")
    console.print(f"[red]Draft rejected: {draft_path.name} → {new_path.name}")
    if reason:
        console.print(f"[yellow]Reason: {reason}")


@cli.command()
@click.argument("draft_id")
@click.argument("new_text")
def edit(draft_id: str, new_text: str) -> None:
    """Edit a draft tweet by ID (works on both new and approved drafts)"""
    # Try to find draft in either new or approved directories
    draft_path = find_draft(draft_id, "new", show_error=False) or find_draft(
        draft_id, "approved"
    )
    if not draft_path:
        return

    draft = TweetDraft.load(draft_path)
    console.print(f"[cyan]Original text: {draft.text}")
    draft.text = new_text
    draft.save(draft_path)
    console.print(f"[green]Draft updated: {draft_path.name}")
    console.print(f"[cyan]New text: {draft.text}")


def _post_tweet_with_thread(client, draft: TweetDraft, tweet_id: str) -> None:
    """Post thread follow-ups after main tweet is posted."""
    if draft.thread:
        console.print(f"[cyan]Posting {len(draft.thread)} thread follow-ups...")
        previous_tweet_id = tweet_id
        for i, thread_text in enumerate(draft.thread):
            try:
                thread_response = client.create_tweet(
                    text=thread_text,
                    in_reply_to_tweet_id=previous_tweet_id,
                    user_auth=False,
                )
                if thread_response.data:
                    previous_tweet_id = thread_response.data["id"]
                    console.print(
                        f"[green]Posted thread tweet {i+1}/{len(draft.thread)}: {previous_tweet_id}"
                    )
                else:
                    console.print(
                        f"[red]Error: No response data for thread tweet {i+1}"
                    )
                    break
            except Exception as e:
                console.print(f"[red]Error posting thread tweet {i+1}: {e}")
                break


def _git_commit_posted(path: Path) -> None:
    """Git add+commit a posted tweet file so duplicate detection survives branch checkouts."""
    import subprocess

    try:
        subprocess.run(
            ["git", "add", str(path)],
            cwd=AGENT_DIR,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                f"chore(twitter): auto-posted {path.name}",
                "--",
                str(path),
            ],
            cwd=AGENT_DIR,
            capture_output=True,
            check=True,
        )
        console.print(f"[green]Committed posted tweet: {path.name}")
    except subprocess.CalledProcessError as e:
        # Non-fatal - log but don't break posting flow
        console.print(
            f"[yellow]Could not auto-commit posted tweet: {e.stderr.decode().strip()}"
        )


@cli.command()
@click.option("--dry-run", is_flag=True, help="Don't actually post tweets")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--draft-id", help="Post a specific draft by ID or path")
def post(dry_run: bool, yes: bool, draft_id: Optional[str] = None) -> None:
    """Post approved tweets"""
    # If a specific draft ID is provided, find only that draft
    if draft_id:
        draft_path = find_draft(draft_id, "approved")
        if not draft_path:
            return
        drafts = [draft_path]
    else:
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

            # Check for already posted duplicates
            duplicates = _check_for_duplicate_replies_internal(draft)
            if "posted" in duplicates:
                console.print(
                    f"\n[red]⚠ WARNING: Already posted {len(duplicates['posted'])} reply(ies) to this tweet![/red]"
                )
                for dup_path in duplicates["posted"]:
                    dup_draft = TweetDraft.load(dup_path)
                    console.print(f"[red]  - Posted: {dup_draft.text[:80]}...[/red]")
                console.print(
                    "[red]This would be a duplicate reply. Consider rejecting instead.[/red]\n"
                )

        console.print(f"[white]{draft.text}")

        # Three possible actions:
        # 1. --dry-run: Just show what would happen
        # 2. --yes: Post without confirmation
        # 3. Neither: Ask for confirmation

        if dry_run:
            console.print("[yellow]Dry run - tweet would be posted")
            console.print(f"[blue]Draft ID: {path.stem}[/blue]")
            continue

        if yes or Confirm.ask("Post this tweet?", default=True):
            try:
                # Post the main tweet
                response = client.create_tweet(
                    text=draft.text,
                    in_reply_to_tweet_id=draft.in_reply_to,
                    user_auth=False,
                )

                if response.data:
                    tweet_id = response.data["id"]
                    console.print(f"[green]Posted tweet: {tweet_id}")

                    # Post thread follow-ups if present
                    _post_tweet_with_thread(client, draft, tweet_id)

                    move_draft(path, "posted")
                else:
                    console.print("[red]Error: No response data from tweet creation")
            except Exception as e:
                console.print(f"[red]Error posting tweet: {e}")
        else:
            console.print("[yellow]Skipped posting tweet")


def should_evaluate_tweet(tweet_text: str, min_length: int = 50) -> Tuple[bool, str]:
    """
    Pre-filter tweets before expensive LLM evaluation.

    Returns:
        (should_evaluate, skip_reason)
    """
    # Skip retweets without commentary
    if tweet_text.strip().startswith("RT @"):
        return False, "retweet"

    # Skip very short tweets (likely not substantial)
    if len(tweet_text.strip()) < min_length:
        return False, f"too short (<{min_length} chars)"

    # Skip pure link tweets (mostly URL)
    # Count URL characters (approximate)
    url_chars = sum(len(word) for word in tweet_text.split() if word.startswith("http"))
    text_chars = len(tweet_text)
    if url_chars > 0 and (url_chars / text_chars) > 0.7:
        return False, "mostly links"

    return True, ""


def process_timeline_tweets(
    tweets,
    users,
    source: str,
    client,
    times: Optional[int] = None,
    dry_run: bool = False,
    max_drafts: Optional[int] = None,
    max_auto_posts: int = 5,
) -> int:
    """Process tweets from timeline

    Returns:
        Number of drafts generated
    """
    drafts_generated = 0
    auto_posts_this_cycle = 0
    tweets_skipped_prefilter = 0

    # Count existing drafts in new/ directory
    existing_drafts = len(list(NEW_DIR.glob("*.yml")))
    total_drafts = existing_drafts + drafts_generated

    # Check if we're already at or over the limit
    # Don't return early - we still need to process trusted user mentions
    at_draft_limit = max_drafts and total_drafts >= max_drafts
    if at_draft_limit:
        console.print(
            f"[yellow]Already have {existing_drafts} drafts (limit: {max_drafts}), will skip drafting for non-trusted users"
        )

    # Create lookup for user info
    user_lookup = {user.id: user for user in users} if users else {}
    tweets_processed = 0

    # Build a set of tweet IDs we already have replies for (across all directories)
    # This is more robust than filename substring matching
    # Use strings for consistent comparison (in_reply_to can be int or str)
    _replied_tweet_ids: set[str] = set()
    for status_dir_name in ["posted", "approved", "new"]:
        status_dir = TWEETS_DIR / status_dir_name
        if not status_dir.exists():
            continue
        for draft_path in status_dir.glob("*.yml"):
            try:
                other_draft = TweetDraft.load(draft_path)
                if other_draft.in_reply_to is not None:
                    _replied_tweet_ids.add(str(other_draft.in_reply_to))
            except Exception:
                continue

    console.print(f"\n[bold]Processing tweets from {source}[/bold]")
    console.print(f"[blue]Existing drafts: {existing_drafts}")
    if max_drafts:
        console.print(f"[blue]Draft limit: {max_drafts}")
    if times:
        console.print(f"[blue]Will process {times} tweet(s)[/blue]")

    for tweet in tweets:
        tweets_processed += 1
        try:
            # Skip our own tweets (using cached function to reduce API calls)
            if tweet.author_id == cached_get_me(client, user_auth=False).data.id:
                continue

            # Check if tweet already has a reply in any directory (posted, approved, new)
            tweet_id_str = str(tweet.id)
            if tweet_id_str in _replied_tweet_ids:
                console.print(
                    f"[yellow]Skip: Already have reply for tweet {tweet_id_str}"
                )
                continue

            # Pre-filter tweet before expensive LLM evaluation
            should_eval, skip_reason = should_evaluate_tweet(tweet.text)
            if not should_eval:
                # Get author username safely for skip message
                author_for_skip = user_lookup.get(tweet.author_id)
                username_for_skip = (
                    author_for_skip.username
                    if author_for_skip
                    else str(tweet.author_id)
                )
                console.print(f"[dim]Skip: {skip_reason} - @{username_for_skip}[/dim]")
                tweets_skipped_prefilter += 1
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
                            "followers": (
                                author.public_metrics["followers_count"]
                                if author
                                else 0
                            ),
                        }
                        if author
                        else {}
                    ),
                },
            }

            # Try to get conversation thread context if this is a reply
            if hasattr(tweet, "conversation_id") and tweet.conversation_id:
                try:
                    thread_tweets = get_conversation_thread(
                        client, tweet.conversation_id
                    )
                    if thread_tweets:
                        # Add thread context at both the root level (for eval/response) and in context (for storage)
                        tweet_data["thread_context"] = thread_tweets
                        tweet_data["context"]["thread_context"] = thread_tweets
                        console.print(
                            f"[blue]Added thread context with {len(thread_tweets)} tweets"
                        )
                except Exception as e:
                    console.print(f"[yellow]Could not retrieve thread context: {e}")

            # Process tweet
            console.print(f"\n[cyan]Processing tweet from @{author_username}:[/cyan]")
            console.print(f"[white]{tweet.text}[/white]")

            # Check if tweet is already cached
            if is_tweet_cached(tweet_id_str):
                console.print(f"[blue]Using cached results for tweet {tweet_id_str}")
                cached_eval, cached_response = load_from_cache(tweet_id_str)

                # Reconstruct objects from cached dictionaries

                if cached_eval:
                    eval_result = EvaluationResponse.from_dict(cached_eval)
                    console.print(
                        f"[blue]Cached Evaluation: {eval_result.action.upper()}"
                    )
                    console.print(
                        f"[blue]Cached Relevance: {eval_result.relevance}/100"
                    )
                    console.print(f"[blue]Cached Priority: {eval_result.priority}/100")
                else:
                    eval_result = None

                response = (
                    TweetResponse.from_dict(cached_response)
                    if cached_response
                    else None
                )
            else:
                # Process tweet and cache results
                eval_result, response = process_tweet(tweet_data)
                save_to_cache(tweet_id_str, eval_result, response)

                # Show evaluation result
                console.print(f"[blue]Evaluation: {eval_result.action.upper()}")
                console.print(f"[blue]Relevance: {eval_result.relevance}/100")
                console.print(f"[blue]Priority: {eval_result.priority}/100")

            if response:
                # Skip error responses from failed LLM parsing
                # Only skip the exact default error text, not legitimate tweets starting with "Error"
                if response.text == "Error processing response":
                    console.print("[yellow]Skipping draft: LLM response parsing failed")
                    continue

                # Create draft from response
                draft = TweetDraft(
                    text=response.text,
                    type=response.type,
                    in_reply_to=tweet.id if response.type == "reply" else None,
                    thread=[response.follow_up]
                    if response.thread_needed and response.follow_up
                    else None,
                    context={
                        "original_tweet": tweet_data,
                        "evaluation": (
                            asdict(eval_result) if eval_result is not None else None
                        ),  # Convert to dict
                        "response_metadata": (
                            asdict(response) if response is not None else None
                        ),  # Convert to dict
                    },
                )

                # Check draft limit before creating main draft
                # Skip this check for trusted users (they get auto-posted, not drafted)
                if not (response.type == "reply" and is_trusted_user(author_username)):
                    total_drafts = existing_drafts + drafts_generated
                    if max_drafts and total_drafts >= max_drafts:
                        console.print(
                            f"[yellow]Reached draft limit ({max_drafts}), skipping draft for non-trusted user"
                        )
                        continue  # Skip to next tweet instead of returning

                if not dry_run:
                    # Check if this is a reply from a trusted user that should be auto-posted
                    if (
                        response.type == "reply"
                        and is_trusted_user(author_username)
                        and eval_result
                        and eval_result.action == "respond"
                    ):
                        # Check for duplicate replies before auto-posting
                        duplicates = _check_for_duplicate_replies_internal(draft)
                        if "posted" in duplicates:
                            console.print(
                                f"[yellow]⚠ Skipping auto-post: already posted {len(duplicates['posted'])} reply(ies) to tweet {draft.in_reply_to}"
                            )
                            continue

                        # Rate limit auto-posts per cycle
                        if auto_posts_this_cycle >= max_auto_posts:
                            console.print(
                                f"[yellow]⚠ Auto-post rate limit reached ({max_auto_posts}/cycle), saving as draft"
                            )
                            path = save_draft(draft, "new")
                            console.print(f"[yellow]Saved as draft: {path}")
                            drafts_generated += 1
                            if draft.in_reply_to is not None:
                                _replied_tweet_ids.add(str(draft.in_reply_to))
                            continue

                        # Auto-post for trusted users
                        console.print(
                            f"[green]Auto-posting reply to trusted user @{author_username}"
                        )
                        try:
                            client_for_post = load_twitter_client(require_auth=True)
                            post_response = client_for_post.create_tweet(
                                text=draft.text,
                                in_reply_to_tweet_id=draft.in_reply_to,
                                user_auth=False,
                            )
                            if post_response.data:
                                tweet_id = post_response.data["id"]
                                console.print(f"[green]✓ Auto-posted tweet: {tweet_id}")

                                # Post thread follow-ups if present
                                _post_tweet_with_thread(
                                    client_for_post, draft, tweet_id
                                )

                                path = save_draft(draft, "posted")
                                console.print(f"[green]Saved to posted: {path}")
                                _git_commit_posted(path)
                                auto_posts_this_cycle += 1
                                _replied_tweet_ids.add(str(tweet.id))
                                # Don't increment drafts_generated for auto-posted tweets
                                # They go to posted/ not new/ and shouldn't count against draft limit
                            else:
                                console.print(
                                    "[red]Error: No response data from auto-post"
                                )
                                # Fall back to saving as new
                                path = save_draft(draft, "new")
                                console.print(
                                    f"[yellow]Saved as draft for manual review: {path}"
                                )
                                drafts_generated += 1
                                if draft.in_reply_to is not None:
                                    _replied_tweet_ids.add(str(draft.in_reply_to))
                        except Exception as e:
                            console.print(f"[red]Error auto-posting: {e}")
                            # Fall back to saving as new
                            path = save_draft(draft, "new")
                            console.print(
                                f"[yellow]Saved as draft for manual review: {path}"
                            )
                            drafts_generated += 1
                            if draft.in_reply_to is not None:
                                _replied_tweet_ids.add(str(draft.in_reply_to))
                    else:
                        # Normal flow - save to new/ for review
                        path = save_draft(draft, "new")
                        console.print(f"[green]Created draft response: {path}")
                        drafts_generated += 1
                        if draft.in_reply_to is not None:
                            _replied_tweet_ids.add(str(draft.in_reply_to))
                else:
                    console.print("[yellow]Would create draft response:")
                    console.print(f"[white]{draft.text}")

                # If thread needed, create follow-up drafts
                if response.thread_needed and response.follow_up:
                    for i, follow_up in enumerate([response.follow_up], 1):
                        # Assert that eval_result is not None before using asdict()
                        assert (
                            eval_result is not None
                        ), "eval_result should not be None in thread context"

                        thread_draft = TweetDraft(
                            text=follow_up,
                            type="thread",
                            context={
                                "thread_position": i,
                                "original_tweet": tweet_data,
                                "evaluation": asdict(eval_result),  # Convert to dict
                            },
                        )
                        if not dry_run:
                            thread_path = save_draft(thread_draft, "new")
                            console.print(
                                f"[green]Created thread draft {i}: {thread_path}"
                            )
                            drafts_generated += 1
                        else:
                            console.print(f"[yellow]Would create thread draft {i}:")
                            console.print(f"[white]{thread_draft.text}")

                        if (
                            not dry_run
                            and max_drafts
                            and drafts_generated >= max_drafts
                        ):
                            console.print(
                                "[yellow]Reached maximum number of drafts, stopping..."
                            )
                            return drafts_generated

            if times and tweets_processed >= times:
                console.print(f"[yellow]Processed {times} tweet(s), stopping...")
                return drafts_generated

        except Exception as e:
            logging.exception(f"Error processing tweet {tweet.id}")
            console.print(f"[red]Error processing tweet {tweet.id}: {e}")
            raise SystemExit(1)

    # Print filtering summary
    console.print("\n[bold cyan]Timeline processing complete[/bold cyan]")
    console.print(f"[blue]Tweets processed: {tweets_processed}")
    console.print(f"[blue]Tweets skipped (pre-filter): {tweets_skipped_prefilter}")
    console.print(f"[blue]Drafts generated: {drafts_generated}")

    return drafts_generated


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
def monitor(
    list_id: Optional[str],
    interval: int,
    dry_run: bool,
    times: Optional[int],
    max_drafts: int = 10,
) -> None:
    """Monitor timeline and generate draft replies

    Args:
        list_id: Optional Twitter list ID to monitor
        interval: Check interval in seconds
        dry_run: If True, don't save drafts
        times: Optional limit on number of checks to run
        max_drafts: Maximum total drafts to maintain (default: 10)
    """
    session_op = metrics.start_operation("monitor_session", "twitter")
    checks_run = 0

    console.print("[green]Starting timeline monitor...")
    console.print(f"[blue]Checking every {interval} seconds")
    console.print(f"[blue]Draft limit: {max_drafts}")

    # Initialize Twitter client
    client = load_twitter_client(require_auth=True)

    def check_timeline():
        """Check timeline and generate drafts"""
        nonlocal checks_run
        checks_run += 1
        check_op = metrics.start_operation("timeline_check", "twitter")

        try:
            # Get timeline tweets
            if list_id:
                tweets = client.get_list_tweets(
                    list_id,
                    tweet_fields=[
                        "created_at",
                        "author_id",
                        "public_metrics",
                        "conversation_id",
                    ],
                    expansions=["author_id"],
                    user_fields=["username", "public_metrics"],
                    user_auth=False,
                )
                source = f"list {list_id}"
            else:
                tweets = client.get_home_timeline(
                    tweet_fields=[
                        "created_at",
                        "author_id",
                        "public_metrics",
                        "conversation_id",
                    ],
                    expansions=["author_id"],
                    user_fields=["username", "public_metrics"],
                    user_auth=False,
                )
                source = "home timeline"

            if not tweets.data:
                console.print("[yellow]No new tweets found")
                check_op.complete(success=True)
                logger.info("Timeline check - no tweets", extra={"source": source})
                return

            # Process tweets
            if dry_run:
                console.print(
                    "[yellow]DRY RUN: Processing tweets but not saving drafts"
                )

            process_timeline_tweets(
                tweets.data,
                tweets.includes["users"] if tweets.includes else None,
                source,
                client,
                times=times,
                dry_run=dry_run,
                max_drafts=max_drafts,  # Use proper max_drafts limit
            )

            check_op.complete(success=True)
            logger.info(
                "Timeline check complete",
                extra={
                    "source": source,
                    "tweets_found": len(tweets.data),
                    "dry_run": dry_run,
                },
            )

        except Exception as e:
            check_op.complete(success=False, error=str(e))
            logger.error("Timeline check failed", extra={"error": str(e)})
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
            session_op.complete(success=True)
            logger.info(
                "Monitor session stopped",
                extra={"checks_run": checks_run, "reason": "keyboard_interrupt"},
            )
    else:
        session_op.complete(success=True)
        logger.info(
            "Monitor session complete",
            extra={"checks_run": checks_run, "single_run": True},
        )


@cli.command()
@click.option("--list-id", help="Twitter list ID to monitor")
@click.option(
    "--auto-approve",
    is_flag=True,
    help="Automatically approve drafts that pass all checks",
)
@click.option(
    "--post-approved", is_flag=True, help="Post approved tweets after reviewing"
)
@click.option(
    "--dry-run", is_flag=True, help="Don't actually save drafts or post tweets"
)
@click.option(
    "--max-tweets", type=int, default=10, help="Maximum number of tweets to process"
)
@click.option(
    "--max-drafts", type=int, default=5, help="Maximum number of drafts to generate"
)
@click.option("--skip-mentions", is_flag=True, help="Skip processing of mentions")
@click.option("--skip-timeline", is_flag=True, help="Skip processing of timeline")
def auto(
    list_id: Optional[str],
    auto_approve: bool,
    post_approved: bool,
    dry_run: bool,
    max_tweets: int,
    max_drafts: int,
    skip_mentions: bool,
    skip_timeline: bool,
) -> None:
    """Run complete automation cycle (monitor, draft, review, approve)"""
    console.print("[bold green]Starting automation cycle...[/bold green]")

    if skip_mentions and skip_timeline:
        console.print(
            "[yellow]Warning: Both mentions and timeline processing are skipped. Nothing to process."
        )
        return

    # Initialize variables to track overall counts
    total_drafts_generated = 0
    total_tweets_processed = 0

    # Step 1: Monitor sources and generate drafts
    console.print("\n[bold]Step 1: Monitoring for new content[/bold]")
    client = load_twitter_client(require_auth=True)

    # Get our user ID for mentions
    me = cached_get_me(client, user_auth=False)
    my_username = me.data.username if me and me.data else None

    # Process mentions first (if not skipped)
    if not skip_mentions:
        console.print("\n[bold]Processing mentions[/bold]")
        try:
            # Get recent mentions
            mentions = client.get_users_mentions(
                me.data.id,
                max_results=max_tweets,
                tweet_fields=[
                    "created_at",
                    "author_id",
                    "public_metrics",
                    "conversation_id",
                ],
                expansions=["author_id", "referenced_tweets.id"],
                user_fields=["username", "public_metrics"],
                user_auth=False,
            )

            if mentions.data:
                console.print(f"Found {len(mentions.data)} mentions to process")
                source = f"mentions of @{my_username}"

                # Process mentions to generate drafts
                drafts_from_mentions = process_timeline_tweets(
                    mentions.data[: max_tweets - total_tweets_processed],
                    mentions.includes["users"] if mentions.includes else None,
                    source,
                    client,
                    times=max_drafts - total_drafts_generated,
                    dry_run=dry_run,
                    max_drafts=max_drafts - total_drafts_generated,
                )

                # Update counters
                total_tweets_processed += min(
                    len(mentions.data), max_tweets - total_tweets_processed
                )
                total_drafts_generated += drafts_from_mentions
            else:
                console.print("[yellow]No mentions found")
        except Exception as e:
            console.print(f"[red]Error processing mentions: {e}")

    # Process timeline (if not skipped and still under limits)
    if (
        not skip_timeline
        and total_tweets_processed < max_tweets
        and total_drafts_generated < max_drafts
    ):
        console.print("\n[bold]Processing timeline[/bold]")
        try:
            # Get timeline tweets
            if list_id:
                tweets = client.get_list_tweets(
                    list_id,
                    tweet_fields=[
                        "created_at",
                        "author_id",
                        "public_metrics",
                        "conversation_id",
                    ],
                    expansions=["author_id"],
                    user_fields=["username", "public_metrics"],
                    user_auth=False,
                )
                source = f"list {list_id}"
            else:
                tweets = client.get_home_timeline(
                    tweet_fields=[
                        "created_at",
                        "author_id",
                        "public_metrics",
                        "conversation_id",
                    ],
                    expansions=["author_id"],
                    user_fields=["username", "public_metrics"],
                    user_auth=False,
                )
                source = "home timeline"

            if tweets.data:
                remaining_tweets = max_tweets - total_tweets_processed
                console.print(
                    f"Processing {min(len(tweets.data), remaining_tweets)} tweets from {source}"
                )

                if dry_run:
                    console.print(
                        "[yellow]DRY RUN: Processing tweets but not saving drafts"
                    )

                drafts_from_timeline = process_timeline_tweets(
                    tweets.data[:remaining_tweets],
                    tweets.includes["users"] if tweets.includes else None,
                    source,
                    client,
                    times=max_drafts - total_drafts_generated,
                    dry_run=dry_run,
                    max_drafts=max_drafts - total_drafts_generated,
                )

                # Update counters
                total_tweets_processed += min(len(tweets.data), remaining_tweets)
                total_drafts_generated += drafts_from_timeline
            else:
                console.print("[yellow]No new tweets found in timeline")
        except Exception as e:
            console.print(f"[red]Error processing timeline: {e}")

    # Step 2: Review drafts
    console.print("\n[bold]Step 2: Reviewing drafts[/bold]")
    drafts = list_drafts("new")

    if not drafts:
        console.print("[yellow]No drafts to review")
        return

    approved_count = 0
    needs_review_count = 0

    for path in drafts:
        draft = TweetDraft.load(path)

        console.print(f"\n[bold]Reviewing draft:[/bold] {path.name}")
        console.print(f"[cyan]Type: {draft.type}")
        if draft.in_reply_to:
            console.print(f"[cyan]Reply to: {draft.in_reply_to}")
        console.print(f"[white]{draft.text}")

        # Get LLM review
        approved_by_llm, review_result = verify_draft(draft.to_dict())

        # Compute and save quality score
        quality_score = compute_quality_score(review_result)
        draft.quality_score = quality_score
        draft.save(path)  # Persist quality_score to file

        # Display review results
        stars = "★" * quality_score + "☆" * (3 - quality_score)
        console.print(f"\n[bold]Quality Score: {stars} ({quality_score}/3)[/bold]")
        console.print("\n[bold]Review Results:[/bold]")
        for criterion, result in review_result.criteria_results.items():
            status = "✅" if result.passed else "❌"
            console.print(f"{status} {criterion}: {result.notes}")

        if review_result.improvements:
            console.print("\n[yellow]Suggested improvements:[/yellow]")
            for improvement in review_result.improvements:
                console.print(f"• {improvement}")

        # Handle auto-approve or mark for human review
        if auto_approve and approved_by_llm:
            if not dry_run:
                move_draft(path, "approved")
                console.print("[green]Draft automatically approved")
            else:
                console.print("[yellow]Would approve draft (dry run)")
            approved_count += 1
        else:
            if approved_by_llm:
                console.print(
                    "[blue]Draft passes checks but requires human review (--auto-approve to skip)"
                )
            else:
                console.print("[yellow]Draft needs improvements and human review")
            needs_review_count += 1

    # Step 3: Post approved tweets if requested
    if post_approved and approved_count > 0 and not dry_run:
        console.print("\n[bold]Step 3: Posting approved tweets[/bold]")

        approved_drafts = list_drafts("approved")
        if not approved_drafts:
            console.print("[yellow]No approved tweets to post")
        else:
            for path in approved_drafts:
                draft = TweetDraft.load(path)

                console.print("\n[bold]Posting tweet:[/bold]")
                console.print(f"[cyan]Type: {draft.type}")
                if draft.in_reply_to:
                    console.print(f"[cyan]Reply to: {draft.in_reply_to}")
                console.print(f"[white]{draft.text}")

                try:
                    response = client.create_tweet(
                        text=draft.text,
                        in_reply_to_tweet_id=draft.in_reply_to,
                        user_auth=False,
                    )

                    if response.data:
                        tweet_id = response.data["id"]
                        console.print(f"[green]Posted tweet: {tweet_id}")

                        # Post thread follow-ups if present
                        _post_tweet_with_thread(client, draft, tweet_id)

                        move_draft(path, "posted")
                    else:
                        console.print(
                            "[red]Error: No response data from tweet creation"
                        )
                except Exception as e:
                    console.print(f"[red]Error posting tweet: {e}")

    # Summary
    console.print("\n[bold]Automation Cycle Summary:[/bold]")
    console.print(f"Tweets processed: {total_tweets_processed}")
    console.print(f"Drafts reviewed: {len(drafts)}")
    console.print(f"Auto-approved: {approved_count}")
    console.print(f"Needs human review: {needs_review_count}")

    if needs_review_count > 0:
        console.print(
            "\n[yellow]Run the following command to review pending drafts:[/yellow]"
        )
        console.print(f"[blue]{sys.argv[0]} review[/blue]")


if __name__ == "__main__":
    cli()
