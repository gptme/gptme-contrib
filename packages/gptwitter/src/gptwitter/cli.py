"""CLI for the gptwitter package."""

from __future__ import annotations

import sys

import click
import tweepy
from gptmail.communication_utils.messaging import (  # type: ignore[import-not-found]
    split_thread,
)

from .api import (
    DEFAULT_LIMIT,
    DEFAULT_SINCE,
    cached_get_me,
    console,
    display_tweet,
    display_tweets,
    format_tweet_stats,
    load_twitter_client,
    parse_time,
)


@click.group()
def cli() -> None:
    """Twitter Tool - Simple CLI for Twitter operations."""
    pass


@cli.command()
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    type=click.IntRange(5, 100),
    help="Number of tweets (5-100)",
)
def me(limit: int) -> None:
    """Read your own recent tweets."""
    client = load_twitter_client(require_auth=True)

    me_result = cached_get_me(client, user_auth=False)
    username = me_result.data.username

    console.print(f"[yellow]Fetching tweets for @{username}")

    try:
        user_result = client.get_user(username=username)
        if not user_result.data:
            console.print(f"[red]User @{username} not found")
            sys.exit(1)
    except tweepy.TweepyException as e:
        console.print(f"[red]Error getting user info: {e}")
        sys.exit(1)

    try:
        tweets = client.get_users_tweets(
            user_result.data.id,
            max_results=limit,
            exclude=["retweets", "replies"],
            tweet_fields=["created_at", "public_metrics"],
        )
        display_tweets(tweets, username)
    except tweepy.TweepyException as e:
        console.print(f"[red]Error getting tweets: {e}")
        sys.exit(1)


@cli.command()
@click.argument("text")
@click.option("--reply-to", help="Tweet ID to reply to")
@click.option("--thread", is_flag=True, help="Post as thread (split by ---)")
def post(text: str, reply_to: str | None, thread: bool) -> None:
    """Post a tweet (requires OAuth authentication)."""
    client = load_twitter_client(require_auth=True)

    if thread:
        thread_messages = split_thread(text)
        reply_to_id: str | None = None

        for message in thread_messages:
            response = client.create_tweet(
                text=message.text,
                in_reply_to_tweet_id=reply_to_id,
                user_auth=False,
            )
            if not response.data:
                console.print("[red]Error: No response data from tweet creation")
                sys.exit(1)

            tweet_data = response.data
            if not isinstance(tweet_data, dict):
                console.print("[red]Error: Unexpected response data format")
                sys.exit(1)

            reply_to_id = tweet_data.get("id")
            if not reply_to_id:
                console.print("[red]Error: Could not get tweet ID from response")
                sys.exit(1)

            console.print(f"[green]Posted tweet: {message.text}")
        return

    response = client.create_tweet(text=text, in_reply_to_tweet_id=reply_to, user_auth=False)
    if not response.data:
        console.print("[red]Error: No response data from tweet creation")
        sys.exit(1)

    tweet_data = response.data
    if not isinstance(tweet_data, dict):
        console.print("[red]Error: Unexpected response data format")
        sys.exit(1)

    tweet_id = tweet_data.get("id")
    if not tweet_id:
        console.print("[red]Error: Could not get tweet ID from response")
        sys.exit(1)

    console.print(f"[green]Posted tweet: {text}")
    console.print(f"[blue]Tweet ID: {tweet_id}")


@cli.command()
@click.argument("username")
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    type=click.IntRange(5, 100),
    help="Number of tweets (5-100)",
)
def user(username: str, limit: int) -> None:
    """Read any user's tweets (uses bearer token)."""
    client = load_twitter_client(require_auth=False)
    username = username.lstrip("@")

    try:
        user_result = client.get_user(username=username)
        if not user_result.data:
            console.print(f"[red]User @{username} not found")
            sys.exit(1)
    except tweepy.TweepyException as e:
        console.print(f"[red]Error getting user info: {e}")
        sys.exit(1)

    try:
        tweets = client.get_users_tweets(
            user_result.data.id,
            max_results=limit,
            exclude=["retweets", "replies"],
            tweet_fields=["created_at", "public_metrics"],
        )
        display_tweets(tweets, username)
    except tweepy.TweepyException as e:
        console.print(f"[red]Error getting tweets: {e}")
        sys.exit(1)


@cli.command()
@click.argument("usernames", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Show what would be done without actually following")
def follow(usernames: tuple[str, ...], dry_run: bool) -> None:
    """Follow one or more Twitter accounts (requires follows.write scope)."""
    client = load_twitter_client(require_auth=True)

    results: dict[str, list[str]] = {
        "followed": [],
        "already_following": [],
        "errors": [],
    }

    for raw_username in usernames:
        username = raw_username.lstrip("@")
        try:
            target = client.get_user(username=username)
            if not target.data:
                console.print(f"[red]User @{username} not found")
                results["errors"].append(username)
                continue

            target_id = target.data.id

            if dry_run:
                console.print(f"[yellow][dry-run] Would follow @{username} (id={target_id})")
                continue

            response = client.follow_user(target_id, user_auth=False)
            if response.data:
                if response.data.get("following"):
                    console.print(f"[green]Now following @{username}")
                    results["followed"].append(username)
                elif response.data.get("pending_follow"):
                    console.print(f"[yellow]Follow request sent to @{username} (protected account)")
                    results["followed"].append(username)
                else:
                    console.print(f"[blue]Already following @{username}")
                    results["already_following"].append(username)
            else:
                console.print(f"[red]Failed to follow @{username}: unexpected response")
                results["errors"].append(username)

        except tweepy.TweepyException as e:
            error_msg = str(e)
            if "402" in error_msg or "Payment Required" in error_msg:
                console.print(
                    f"[red]API credits exhausted — cannot follow @{username}.\n"
                    "The X API uses pay-as-you-go billing. Add credits at https://developer.x.com "
                    "or follow accounts manually via the web UI."
                )
                results["errors"].append(username)
                remaining = [
                    name.lstrip("@")
                    for name in usernames
                    if name.lstrip("@")
                    not in results["followed"] + results["already_following"] + results["errors"]
                ]
                if remaining:
                    console.print(
                        f"[yellow]Skipping {len(remaining)} remaining account(s): "
                        f"{', '.join('@' + name for name in remaining)}"
                    )
                    results["errors"].extend(remaining)
                break
            if "403" in error_msg or "not authorized" in error_msg.lower():
                console.print(
                    f"[red]Authorization error for @{username}: missing 'follows.write' scope?\n"
                    "Re-run OAuth with the updated scope or follow manually via the web UI."
                )
                results["errors"].append(username)
                continue

            console.print(f"[red]Failed to follow @{username}: {e}")
            results["errors"].append(username)

    if not dry_run:
        console.print(
            f"\n[bold]Summary:[/bold] followed={len(results['followed'])}, "
            f"already_following={len(results['already_following'])}, "
            f"errors={len(results['errors'])}"
        )
        if results["errors"]:
            console.print(f"[red]Errors: {', '.join(results['errors'])}")


@cli.command()
@click.argument("username")
@click.option("--since", default=DEFAULT_SINCE, help="Time window (e.g. 24h, 7d)")
@click.option("--limit", default=DEFAULT_LIMIT, help="Maximum number of mentions to fetch")
def mentions(username: str, since: str, limit: int) -> None:
    """Check mentions of a specific user."""
    client = load_twitter_client(require_auth=False)
    username = username.lstrip("@")

    user_result = client.get_user(username=username)
    if not user_result.data:
        console.print(f"[red]User @{username} not found")
        sys.exit(1)

    start_time = parse_time(since)
    query = f"@{username} -from:{username}"
    mentions_result = client.search_recent_tweets(
        query=query,
        max_results=limit,
        start_time=start_time,
        tweet_fields=["created_at", "author_id", "public_metrics"],
        expansions=["author_id"],
        user_fields=["username"],
    )

    if not mentions_result.data:
        console.print(f"[yellow]No mentions found for @{username}")
        return

    users = (
        {user.id: user for user in mentions_result.includes["users"]}
        if mentions_result.includes
        else {}
    )

    console.print(f"\n[bold]Recent mentions of @{username}:[/bold]\n")
    for tweet in mentions_result.data:
        author = users.get(tweet.author_id)
        author_name = f"@{author.username}" if author else str(tweet.author_id)
        display_tweet(tweet, author_name)


@cli.command()
@click.option("--since", default=DEFAULT_SINCE, help="Time window (e.g. 24h, 7d)")
@click.option("--limit", default=DEFAULT_LIMIT, help="Maximum number of replies to fetch")
@click.option("--unanswered", is_flag=True, help="Show only unanswered tweets")
def replies(since: str, limit: int, unanswered: bool) -> None:
    """Check replies to our tweets."""
    client = load_twitter_client(require_auth=True)

    me_result = cached_get_me(client, user_auth=False)
    if not me_result.data:
        console.print("[red]Could not get user information")
        sys.exit(1)

    start_time = parse_time(since)
    mentions_result = client.get_users_mentions(
        me_result.data.id,
        max_results=limit,
        start_time=start_time,
        user_auth=False,
        expansions=["author_id", "in_reply_to_user_id"],
        tweet_fields=["created_at", "author_id", "public_metrics"],
    )

    tweets = [tweet for tweet in mentions_result.data or []]
    if unanswered:
        tweets = [tweet for tweet in tweets if tweet.public_metrics["reply_count"] == 0]

    if not tweets:
        console.print("[yellow]No replies found")
        return

    console.print("\n[bold]Recent Replies:[/bold]\n")
    for tweet in tweets:
        author_info = f"@{tweet.author_id}"
        if mentions_result.includes and "users" in mentions_result.includes:
            for user in mentions_result.includes["users"]:
                if user.id == tweet.author_id:
                    author_info = f"@{user.username}"
                    break

        display_tweet(tweet, author_info)


@cli.command()
@click.option("--since", default=DEFAULT_SINCE, help="Time window (e.g. 24h, 7d)")
@click.option("--limit", default=DEFAULT_LIMIT, help="Maximum number of replies to fetch")
@click.option("--unanswered", is_flag=True, help="Show only unanswered tweets")
def quotes(since: str, limit: int, unanswered: bool) -> None:
    """Check quotes of our tweets."""
    client = load_twitter_client(require_auth=True)

    me_result = cached_get_me(client, user_auth=False)
    if not me_result.data:
        console.print("[red]Could not get user information")
        sys.exit(1)

    start_time = parse_time(since)
    query = f"url:{me_result.data.username}"
    quotes_result = client.search_recent_tweets(
        query=query,
        max_results=limit,
        start_time=start_time,
        tweet_fields=["created_at", "author_id", "public_metrics"],
        expansions=["author_id"],
        user_fields=["username"],
    )

    tweets = [tweet for tweet in quotes_result.data or []]
    if unanswered:
        tweets = [tweet for tweet in tweets if tweet.public_metrics["reply_count"] == 0]

    if not tweets:
        console.print("[yellow]No quotes found")
        return

    users = (
        {user.id: user for user in quotes_result.includes["users"]}
        if quotes_result.includes
        else {}
    )

    console.print("\n[bold]Recent Quotes:[/bold]")
    for tweet in tweets:
        author = users.get(tweet.author_id)
        author_name = f"@{author.username}" if author else str(tweet.author_id)
        stats = format_tweet_stats(tweet)

        console.print(f"\n[cyan]{tweet.created_at.strftime('%Y-%m-%d %H:%M')}[/cyan]")
        console.print(f"[green]From: {author_name}[/green]")
        console.print(f"[white]Quote: {tweet.text}[/white]")
        console.print(f"[blue]Stats: {stats}[/blue]")
        console.print(f"[magenta]ID: {tweet.id}[/magenta]")
        console.print("─" * 50)


@cli.command()
@click.option("--since", default=DEFAULT_SINCE, help="Time window (e.g. 24h, 7d)")
@click.option("--limit", default=DEFAULT_LIMIT, help="Maximum number of tweets to fetch")
@click.option("--list-id", help="Twitter list ID to fetch from")
def timeline(since: str, limit: int, list_id: str | None) -> None:
    """Read home timeline or list timeline."""
    client = load_twitter_client(require_auth=True)

    start_time = parse_time(since)
    if list_id:
        try:
            tweets = client.get_list_tweets(
                list_id,
                max_results=limit,
                start_time=start_time,
                tweet_fields=["created_at", "author_id", "public_metrics"],
                expansions=["author_id"],
                user_fields=["username"],
                user_auth=False,
            )
            source = f"list {list_id}"
        except tweepy.TweepyException as e:
            console.print(f"[red]Error getting list tweets: {e}")
            sys.exit(1)
    else:
        try:
            tweets = client.get_home_timeline(
                max_results=limit,
                start_time=start_time,
                tweet_fields=["created_at", "author_id", "public_metrics"],
                expansions=["author_id"],
                user_fields=["username"],
                user_auth=False,
            )
            source = "home timeline"
        except tweepy.TweepyException as e:
            console.print(f"[red]Error getting timeline: {e}")
            sys.exit(1)

    if not tweets.data:
        console.print(f"[yellow]No tweets found in {source}")
        return

    users = {user.id: user for user in tweets.includes["users"]} if tweets.includes else {}

    console.print(f"\n[bold]Recent tweets from {source}:[/bold]\n")
    for tweet in tweets.data:
        author = users.get(tweet.author_id)
        author_name = f"@{author.username}" if author else str(tweet.author_id)
        display_tweet(tweet, author_name)


@cli.command()
@click.argument("tweet_id")
@click.option("--limit", default=100, help="Maximum number of tweets to fetch per page")
@click.option("--max-pages", default=5, help="Maximum number of pagination pages to fetch")
@click.option("--verbose", is_flag=True, help="Show detailed debug information")
@click.option("--structure", is_flag=True, help="Show thread structure with indentation")
def thread(tweet_id: str, limit: int, max_pages: int, verbose: bool, structure: bool) -> None:
    """Read a complete conversation thread given a tweet ID."""
    client = load_twitter_client(require_auth=False)

    if verbose:
        console.print(f"[blue]Fetching tweet with ID: {tweet_id}")

    try:
        tweet = client.get_tweet(
            tweet_id,
            tweet_fields=[
                "created_at",
                "author_id",
                "public_metrics",
                "conversation_id",
                "in_reply_to_user_id",
                "referenced_tweets",
            ],
            expansions=["author_id", "referenced_tweets.id", "in_reply_to_user_id"],
            user_fields=["username", "name", "profile_image_url"],
        )
    except Exception as e:
        console.print(f"[red]Error fetching original tweet: {e}")
        return

    if not tweet.data:
        console.print("[yellow]Tweet not found")
        return

    conversation_id = tweet.data.conversation_id or tweet_id
    if verbose:
        console.print(f"[blue]Found conversation ID: {conversation_id}")
        console.print(
            f"[blue]Retrieving conversation thread with pagination (max {max_pages} pages)"
        )

    all_tweets = {tweet.data.id: tweet.data}
    all_users = {}
    next_token = None
    page_count = 0

    if tweet.includes and "users" in tweet.includes:
        all_users.update({user.id: user for user in tweet.includes["users"]})

    while page_count < max_pages:
        try:
            kwargs = {
                "query": f"conversation_id:{conversation_id}",
                "max_results": limit,
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
                "user_fields": ["username", "name", "profile_image_url"],
            }

            if next_token:
                kwargs["next_token"] = next_token

            conversation = client.search_recent_tweets(**kwargs)
            page_count += 1

            if verbose:
                console.print(f"[blue]Retrieved page {page_count} of conversation thread")

            if conversation.data:
                if verbose:
                    console.print(f"[blue]Found {len(conversation.data)} tweets on this page")
                for reply in conversation.data:
                    all_tweets[reply.id] = reply

                if conversation.includes and "users" in conversation.includes:
                    all_users.update({user.id: user for user in conversation.includes["users"]})

            if not hasattr(conversation, "meta") or "next_token" not in conversation.meta:
                if verbose:
                    console.print("[blue]No more pages available")
                break

            next_token = conversation.meta["next_token"]

        except Exception as e:
            console.print(f"[red]Error retrieving conversation page {page_count + 1}: {e}")
            break

    if not all_tweets:
        console.print("[yellow]No tweets found in conversation")
        return

    console.print(f"\n[bold]Conversation Thread[/bold] (ID: {conversation_id})")
    console.print(f"[blue]Retrieved {len(all_tweets)} tweets from {page_count} page(s)")

    sorted_tweets = sorted(all_tweets.values(), key=lambda tweet_obj: tweet_obj.created_at)

    if structure:
        tweets_by_id = {tweet_obj.id: tweet_obj for tweet_obj in sorted_tweets}

        reply_to = {}
        for tweet_obj in sorted_tweets:
            if hasattr(tweet_obj, "referenced_tweets") and tweet_obj.referenced_tweets:
                for ref in tweet_obj.referenced_tweets:
                    if ref.type == "replied_to":
                        reply_to[tweet_obj.id] = ref.id
                        break

        root_id = sorted_tweets[0].id
        for tweet_obj in sorted_tweets:
            if not hasattr(tweet_obj, "referenced_tweets") or not tweet_obj.referenced_tweets:
                root_id = tweet_obj.id
                break

        console.print("\n[bold]Thread Structure:[/bold]\n")
        displayed = set()

        def display_thread(tweet_id_to_show: object, level: int = 0) -> None:
            if tweet_id_to_show in displayed or tweet_id_to_show not in tweets_by_id:
                return

            tweet_obj = tweets_by_id[tweet_id_to_show]
            displayed.add(tweet_id_to_show)

            author = all_users.get(tweet_obj.author_id)
            author_name = f"@{author.username}" if author else str(tweet_obj.author_id)
            indent = "  " * level

            console.print(f"{indent}[cyan]{tweet_obj.created_at.strftime('%Y-%m-%d %H:%M')}[/cyan]")
            console.print(f"{indent}[green]From: {author_name}[/green]")
            console.print(f"{indent}[white]{tweet_obj.text}[/white]")

            stats = format_tweet_stats(tweet_obj)
            if stats:
                console.print(f"{indent}[blue]Stats: {stats}[/blue]")

            replies_to_show = [
                child_id
                for child_id, reply_to_id in reply_to.items()
                if reply_to_id == tweet_id_to_show
            ]
            for reply_id in replies_to_show:
                display_thread(reply_id, level + 1)

        display_thread(root_id)

        remaining = set(tweets_by_id.keys()) - displayed
        if remaining:
            console.print(
                "\n[yellow]Additional tweets in conversation (structure unclear):[/yellow]"
            )
            for remaining_id in sorted(
                [tweet_id_value for tweet_id_value in remaining],
                key=lambda tweet_id_value: tweets_by_id[tweet_id_value].created_at,
            ):
                display_thread(remaining_id, 0)
        return

    console.print("\n[bold]Chronological Order:[/bold]\n")
    for tweet_obj in sorted_tweets:
        author = all_users.get(tweet_obj.author_id)
        author_name = f"@{author.username}" if author else str(tweet_obj.author_id)
        display_tweet(tweet_obj, author_name)


def main() -> None:
    """Run the gptwitter CLI."""
    cli()
