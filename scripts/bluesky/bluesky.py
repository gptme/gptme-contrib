#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "atproto>=0.0.31",
#   "rich>=13.0.0",
#   "python-dotenv>=1.0.0",
#   "click>=8.0.0",
# ]
# [tool.uv]
# exclude-newer = "2025-02-05T00:00:00Z"
# ///
"""
Bluesky Tool - Simple CLI for Bluesky operations

A command-line interface for Bluesky social network, supporting posting, reading, and interaction management.

Setup:
1. Copy bluesky.env.example to .env:
   cp bluesky.env.example .env

2. Create an app password:
   - Go to Bluesky Settings > App Passwords
   - Click "Create app password"
   - Name it (e.g., "CLI Tool")
   - Copy the generated password

3. Edit .env with your credentials:
   - BLUESKY_HANDLE: Your full handle (e.g., username.bsky.social)
   - BLUESKY_PASSWORD: Your app password

Usage Examples:
    # Basic posting
    ./bluesky.py post "Hello, Bluesky!"

    # Reply to a post
    ./bluesky.py post --reply-to "at://did:plc:xyz/app.bsky.feed.post/123" "My reply"

    # Read posts
    ./bluesky.py me                         # Your recent posts
    ./bluesky.py user handle.bsky.social    # Another user's posts
    ./bluesky.py replies                    # Check your replies
    ./bluesky.py feed                       # Your feed

    # Customize output
    ./bluesky.py me --limit 20              # Show more posts
    ./bluesky.py replies --unanswered       # Show only unanswered replies

Notes:
- Posts are called "skeets" in Bluesky terminology
- Rate limits apply to API calls
- Some operations require authentication
- Use --help with any command for more options
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Any, Dict

import click
from atproto import Client, models, client_utils  # type: ignore
from dotenv import load_dotenv
from rich.console import Console


def validate_post_uri(uri: str) -> Tuple[str, str, str]:
    """Validate and parse a post URI into its components"""
    try:
        if not uri.startswith("at://"):
            raise ValueError("URI must start with 'at://'")

        parts = uri.split("/")
        if len(parts) != 5:
            raise ValueError("Invalid URI format")

        repo = parts[2]  # did:plc:...
        collection = parts[3]  # app.bsky.feed.post
        rkey = parts[4]  # post ID

        if not repo.startswith("did:"):
            raise ValueError("Invalid DID format")
        if collection != "app.bsky.feed.post":
            raise ValueError("Invalid collection - must be 'app.bsky.feed.post'")

        return repo, collection, rkey
    except Exception as e:
        raise ValueError(f"Invalid post URI: {e}")


# Initialize rich console
console = Console()

# Constants
DEFAULT_LIMIT = 10
DEFAULT_SINCE = "7d"


def load_bluesky_client() -> Client:
    """Initialize Bluesky client with credentials from .env"""
    load_dotenv()

    # Check for required credentials
    handle = os.getenv("BLUESKY_HANDLE")
    password = os.getenv("BLUESKY_PASSWORD")

    if not handle or not password:
        console.print("[red]Missing BLUESKY_HANDLE or BLUESKY_PASSWORD in .env")
        sys.exit(1)

    # Create client
    client = Client()
    try:
        client.login(handle, password)
        console.print(f"[green]Successfully logged in as {handle}")
        return client
    except Exception as e:
        console.print(f"[red]Login failed: {e}")
        sys.exit(1)


def format_post_stats(post: models.AppBskyFeedDefs.PostView) -> str:
    """Format engagement stats for a post"""
    stats = []
    if hasattr(post, "likeCount"):
        stats.append(f"💟 {post.likeCount}" if post.likeCount > 0 else "")
    if hasattr(post, "replyCount"):
        stats.append(f"💬 {post.replyCount}" if post.replyCount > 0 else "")
    if hasattr(post, "repostCount"):
        stats.append(f"🔁 {post.repostCount}" if post.repostCount > 0 else "")
    return " ".join(s for s in stats if s)


def format_post_time(created_at: str) -> str:
    """Format post timestamp"""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "N/A"


def display_post(feed_view, author_info: Optional[str] = None) -> None:
    """Display a single post with consistent formatting"""
    # For timeline view, the post is nested in feed_view.post
    post = getattr(feed_view, "post", feed_view)
    record = post.record
    author = post.author

    console.print(f"[cyan]{format_post_time(record.created_at)}[/cyan]")

    # Show author info either from parameter or post
    display_author = author_info or f"{author.display_name} (@{author.handle})"
    console.print(f"[green]From: {display_author}[/green]")

    console.print(f"[magenta]Post URI: {post.uri}[/magenta]")
    console.print(f"[white]Post: {record.text}[/white]")

    # Format engagement stats
    stats = []
    if hasattr(post, "like_count"):
        stats.append(f"💟 {post.like_count}" if post.like_count > 0 else "")
    if hasattr(post, "reply_count"):
        stats.append(f"💬 {post.reply_count}" if post.reply_count > 0 else "")
    if hasattr(post, "repost_count"):
        stats.append(f"🔁 {post.repost_count}" if post.repost_count > 0 else "")

    if stats:
        console.print(f"[blue]Stats: {' '.join(s for s in stats if s)}[/blue]")
    console.print("─" * 50)


def display_posts(feed: models.AppBskyFeedGetAuthorFeed.Response, handle: str) -> None:
    """Display posts in a consistent format"""
    if not feed or not feed.feed:
        console.print(f"[yellow]No posts found for {handle}")
        return

    console.print(f"\n[bold]Recent posts from {handle}:[/bold]\n")
    for post in feed.feed:
        display_post(post.post)


@click.group()
def cli() -> None:
    """Bluesky Tool - Simple CLI for Bluesky operations"""
    pass


@cli.command()
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    type=click.IntRange(1, 100),
    help="Number of posts to fetch (max: 100)",
)
@click.option(
    "--cursor",
    default=None,
    help="Pagination cursor for older posts (obtained from previous request)",
)
def feed(limit: int, cursor: Optional[str]) -> None:
    """Show your Bluesky feed (posts from accounts you follow)

    Displays posts from accounts you follow, including:
    - Post content
    - Author information
    - Engagement stats (likes, replies, reposts)
    - Timestamps

    Use --limit to control how many posts to fetch
    Use --cursor to get older posts (the cursor is shown at the end of results)
    """
    client = load_bluesky_client()

    try:
        # Get timeline using app.bsky.feed namespace
        feed = client.app.bsky.feed.get_timeline({"limit": limit, "cursor": cursor})

        if not feed or not feed.feed:
            console.print("[yellow]No posts found in feed")
            return

        console.print("\n[bold]Your feed:[/bold]\n")

        for item in feed.feed:
            display_post(item)

        # Show cursor for next page if available
        if hasattr(feed, "cursor"):
            console.print("\n[blue]For older posts, use:[/blue]")
            console.print(f"[green]./scripts/bluesky.py feed --cursor {feed.cursor}[/green]")
    except Exception as e:
        console.print(f"[red]Error getting feed: {e}")
        sys.exit(1)


@cli.command()
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    type=click.IntRange(1, 100),
    help="Number of posts to fetch (max: 100)",
)
@click.option(
    "--cursor",
    default=None,
    help="Pagination cursor for older posts (obtained from previous request)",
)
def me(limit: int, cursor: Optional[str]) -> None:
    """Show your own Bluesky posts and reposts

    Displays your recent activity, including:
    - Your original posts
    - Your reposts (showing original author and content)
    - Engagement stats (likes, replies, reposts)
    - Timestamps

    Use --limit to control how many posts to fetch
    Use --cursor to get older posts (the cursor is shown at the end of results)
    """
    client = load_bluesky_client()

    try:
        # Get profile using app.bsky.actor namespace
        profile = client.app.bsky.actor.get_profile({"actor": client.me.did})
        handle = profile.handle

        console.print(f"[yellow]Fetching posts for {handle}")

        # Get author feed using app.bsky.feed namespace
        feed = client.app.bsky.feed.get_author_feed({"actor": client.me.did, "limit": limit, "cursor": cursor})

        if not feed or not feed.feed:
            console.print("[yellow]No posts found in timeline")
            return

        console.print("\n[bold]Your timeline:[/bold]\n")

        for item in feed.feed:
            display_post(item)  # Pass the feed item directly

        # Show cursor for next page if available
        if hasattr(feed, "cursor"):
            console.print("\n[blue]For older posts, use:[/blue]")
            console.print(f"[green]./scripts/bluesky.py me --cursor {feed.cursor}[/green]")
    except Exception as e:
        console.print(f"[red]Error getting posts: {e}")
        sys.exit(1)


def process_post_text(client: Client, text: str) -> client_utils.TextBuilder:
    """Process post text to handle mentions, links, and tags."""
    text_builder = client_utils.TextBuilder()

    # Split text and process each part
    words = text.split()
    for i, word in enumerate(words):
        if word.startswith(("http://", "https://")):
            text_builder.link(word, word)
        elif word.startswith("#"):
            text_builder.tag(word, word[1:])
        elif word.startswith("@"):
            handle = word[1:]  # Remove @ for resolution
            try:
                # Resolve handle to DID, but keep @ in display text
                resolved = client.resolve_handle(handle)
                text_builder.mention(word, resolved.did)
            except Exception as e:
                console.print(f"[yellow]Warning: Could not resolve handle {handle}: {str(e)}")
                text_builder.text(word)
        else:
            text_builder.text(word)

        # Add space between parts, except for the last one
        if i < len(words) - 1:
            text_builder.text(" ")

    return text_builder


def get_post_refs(client: Client, post_uri: str) -> dict:
    """Get the URI and CID for a post"""
    try:
        # Parse the URI to get repo, collection, and rkey
        parts = post_uri.split("/")
        repo = parts[2]  # did:plc:...
        collection = parts[3]  # app.bsky.feed.post
        rkey = parts[4]  # post ID

        # Get the post record using repo.getRecord
        record = client.com.atproto.repo.get_record({"repo": repo, "collection": collection, "rkey": rkey})

        return {"uri": post_uri, "cid": record.cid}
    except Exception as e:
        console.print(f"[red]Error getting post references: {e}")
        sys.exit(1)


def get_reply_refs(client: Client, parent_uri: str) -> dict:
    """Get the root and parent references for a reply"""
    try:
        # First validate the URI format
        repo, collection, rkey = validate_post_uri(parent_uri)

        try:
            # Get the parent post details
            parent = get_post_refs(client, parent_uri)

            # Get the parent post record
            parent_record = client.com.atproto.repo.get_record(
                {"repo": repo, "collection": collection, "rkey": rkey}
            ).value

            # Check if parent post has reply refs
            if isinstance(parent_record, dict) and "reply" in parent_record:
                try:
                    # Use parent's root reference
                    root = {"uri": parent_record["reply"]["root"]["uri"], "cid": parent_record["reply"]["root"]["cid"]}
                    # Validate the root URI format
                    validate_post_uri(root["uri"])
                except (KeyError, ValueError):
                    console.print("[yellow]Warning: Invalid root reference in parent post, using parent as root")
                    root = parent
            else:
                # Parent is the root
                root = parent

            # Verify both references exist and are valid
            if not root.get("uri") or not root.get("cid"):
                raise ValueError("Missing root reference data")
            if not parent.get("uri") or not parent.get("cid"):
                raise ValueError("Missing parent reference data")

            return {"root": root, "parent": parent}
        except ValueError as e:
            raise ValueError(f"Invalid reply reference: {e}")
        except Exception as e:
            if "Could not locate record" in str(e):
                raise ValueError(f"Parent post not found: {parent_uri}")
            raise
    except ValueError as e:
        console.print(f"[red]{str(e)}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error setting up reply: {e}")
        sys.exit(1)


@cli.command()
@click.argument("text")
@click.option("--reply-to", help="Post URI to reply to")
@click.option("--thread", is_flag=True, help="Post as thread (split by ---)")
@click.option("--stdin", is_flag=True, help="Read text from stdin")
def post(text: str, reply_to: Optional[str], thread: bool, stdin: bool) -> None:
    """Post to Bluesky (requires authentication)

    Examples:
        bluesky.py post "Hello world!"                    # Simple post
        bluesky.py post "Hello @user #bluesky"           # Post with mention and tag
        echo "Hello" | bluesky.py post --stdin           # Post from stdin
        bluesky.py post --reply-to "at://..." "My reply" # Reply to a post
        bluesky.py post --thread "First post
        ---
        Second post"                                      # Thread posting
    """
    client = load_bluesky_client()

    # Handle stdin if requested
    if stdin:
        if not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        else:
            console.print("[red]Error: No input provided to stdin")
            sys.exit(1)

    if not text:
        console.print("[red]Error: No text provided")
        sys.exit(1)

    # Handle thread posting
    if thread:
        post_texts = text.split("\n---\n")
        reply_to_uri: Optional[str] = None

        for post_text in post_texts:
            try:
                # Create post record
                thread_post_record: Dict[str, Any] = {
                    "text": post_text.strip(),
                    "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }

                # Add reply refs if this is a reply
                if reply_to_uri:
                    reply_refs = get_reply_refs(client, reply_to_uri)
                    thread_post_record["reply"] = reply_refs

                # Create post
                response = client.com.atproto.repo.create_record(
                    {"repo": client.me.did, "collection": "app.bsky.feed.post", "record": thread_post_record}
                )

                reply_to_uri = response.uri
                console.print(f"[green]Posted: {post_text.strip()}")
                console.print(f"[blue]Post URI: {response.uri}")
            except Exception as e:
                console.print(f"[red]Error posting: {e}")
                sys.exit(1)
    else:
        # Single post
        try:
            # Create post record
            post_record: Dict[str, Any] = {
                "$type": "app.bsky.feed.post",
                "text": text,
                "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }

            # Add reply refs if this is a reply
            if reply_to:
                try:
                    console.print(f"[yellow]Setting up reply to: {reply_to}")
                    reply_refs = get_reply_refs(client, reply_to)
                    post_record["reply"] = reply_refs
                    console.print("[green]Reply references validated successfully")
                except Exception as e:
                    console.print(f"[red]Failed to set up reply: {e}")
                    sys.exit(1)

            try:
                # Create post
                response = client.com.atproto.repo.create_record(
                    {"repo": client.me.did, "collection": "app.bsky.feed.post", "record": post_record}
                )

                console.print("[green]Successfully posted:[/green]")
                console.print(f"[white]{text}[/white]")
                console.print(f"[blue]Post URI: {response.uri}")
                if hasattr(response, "cid"):
                    console.print(f"[blue]CID: {response.cid}")

                # Show link to post
                post_id = response.uri.split("/")[-1]
                post_url = f"https://bsky.app/profile/{client.me.handle}/post/{post_id}"
                console.print(f"[dim]View at: {post_url}[/dim]")

            except Exception as e:
                console.print(f"[red]Error creating post: {e}")
                sys.exit(1)
        except Exception as e:
            console.print(f"[red]Error: {e}")
            sys.exit(1)


@cli.command()
@click.argument("handle")
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    type=click.IntRange(5, 100),
    help="Number of posts to fetch (min: 5, max: 100)",
)
@click.option(
    "--cursor",
    default=None,
    help="Pagination cursor for older posts (obtained from previous request)",
)
def user(handle: str, limit: int, cursor: Optional[str]) -> None:
    """Show posts from a specific Bluesky user

    Displays posts from the specified user, including:
    - Post content
    - Engagement stats (likes, replies, reposts)
    - Timestamps

    Example: bluesky.py user theverge.com
    """
    client = load_bluesky_client()

    try:
        # First resolve the handle to make sure it exists
        try:
            resolved = client.resolve_handle(handle)
            did = resolved.did
        except Exception:
            console.print(f"[red]Error: User '{handle}' not found")
            return

        # Get author feed using proper namespace
        feed = client.app.bsky.feed.get_author_feed(
            {
                "actor": did,  # Use DID instead of handle for better reliability
                "limit": limit,
                "cursor": cursor,
            }
        )

        if not feed or not feed.feed:
            console.print(f"[yellow]No posts found for {handle}")
            return

        console.print(f"\n[bold]Posts from {handle}:[/bold]\n")

        for item in feed.feed:
            display_post(item)  # Pass the feed item directly

        # Show cursor for next page if available
        if hasattr(feed, "cursor"):
            console.print("\n[blue]For older posts, use:[/blue]")
            console.print(f"[green]./scripts/bluesky.py user {handle} --cursor {feed.cursor}[/green]")
    except Exception as e:
        console.print(f"[red]Error getting posts: {e}")
        sys.exit(1)


@cli.command()
@click.option("--since", help="Time window (e.g. 24h, 7d, 30d)", default=None)
@click.option(
    "--limit", default=DEFAULT_LIMIT, type=click.IntRange(5, 100), help="Number of replies to fetch (min: 5, max: 100)"
)
@click.option("--unanswered", is_flag=True, help="Show only replies that you haven't responded to")
def replies(since: str, limit: int, unanswered: bool) -> None:
    """Show replies to your posts

    Displays replies from other users to your posts, including:
    - Reply content
    - Author information
    - Timestamps
    - Original post context

    Use --since to filter by time (e.g. 24h, 7d, 30d)
    Use --unanswered to see only replies you haven't responded to
    """
    client = load_bluesky_client()

    try:
        # Convert since parameter to timestamp
        if since:
            try:
                # Parse time window (e.g., "24h", "7d", "30d")
                value = int(since[:-1])
                unit = since[-1].lower()

                if unit == "h":
                    delta = timedelta(hours=value)
                elif unit == "d":
                    delta = timedelta(days=value)
                else:
                    raise ValueError(f"Invalid time unit: {unit}")

                since_time = datetime.now(timezone.utc) - delta
            except (ValueError, IndexError):
                console.print(f"[red]Error: Invalid time format '{since}'. Use format: 24h, 7d, etc.")
                return
        else:
            since_time = None

        # Get notifications using proper namespace
        notifications = client.app.bsky.notification.list_notifications({"limit": limit})

        # Filter notifications by time if needed
        if since_time:
            notifications.notifications = [
                notif
                for notif in notifications.notifications
                if datetime.fromisoformat(notif.indexed_at.replace("Z", "+00:00")) > since_time
            ]

        # Filter for replies
        replies = [notif for notif in notifications.notifications if notif.reason == "reply"]

        if unanswered:
            # Filter for posts without replies
            replies = [
                reply for reply in replies if not hasattr(reply.record, "replyCount") or reply.record.replyCount == 0
            ]

        # Show filter summary
        filters = []
        if since:
            filters.append(f"from last {since}")
        if unanswered:
            filters.append("unanswered only")
        filter_text = f" ({', '.join(filters)})" if filters else ""

        if not replies:
            console.print(f"[yellow]No replies found{filter_text}")
            return

        # Display replies
        console.print(f"\n[bold]Replies{filter_text}:[/bold]\n")

        for reply in replies:
            try:
                # Show the reply with all information
                author_name = getattr(reply.author, "displayName", None) or reply.author.handle
                author_info = f"{author_name} (@{reply.author.handle})"

                # Format timestamp and URL
                timestamp = format_post_time(reply.indexed_at)
                post_id = reply.uri.split("/")[-1]
                post_url = f"https://bsky.app/profile/{reply.author.handle}/post/{post_id}"

                # Display reply with all information
                console.print(f"[green]{author_info}[/green] • [blue]{timestamp}[/blue]")
                console.print(f"[white]{reply.record.text}[/white]")
                console.print(f"[dim]{post_url}[/dim]")

                # Show engagement stats if any
                stats = []
                if hasattr(reply, "like_count") and reply.like_count > 0:
                    stats.append(f"💟 {reply.like_count}")
                if hasattr(reply, "reply_count") and reply.reply_count > 0:
                    stats.append(f"💬 {reply.reply_count}")
                if hasattr(reply, "repost_count") and reply.repost_count > 0:
                    stats.append(f"🔁 {reply.repost_count}")

                if stats:
                    console.print(f"[blue]{' '.join(stats)}[/blue]")

                console.print("─" * 50)
            except AttributeError as e:
                # If there's an error processing a reply, skip it
                console.print(f"[yellow]Warning: Could not process reply: {str(e)}[/yellow]")

    except Exception as e:
        console.print(f"[red]Error getting replies: {e}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
