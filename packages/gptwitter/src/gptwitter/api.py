"""Shared Twitter/X client helpers for gptme agents."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

import tweepy
from dotenv import load_dotenv
from gptmail.communication_utils.auth import (  # type: ignore[import-not-found]
    run_oauth_callback,
    save_token_to_env,
)
from gptmail.communication_utils.auth.oauth import (  # type: ignore[import-not-found]
    OAuthManager,
)
from gptmail.communication_utils.auth.tokens import (  # type: ignore[import-not-found]
    TokenInfo,
)
from rich.console import Console

DEFAULT_SINCE = "7d"
DEFAULT_LIMIT = 10

console = Console()


@lru_cache(maxsize=1)
def cached_get_me(client: tweepy.Client, user_auth: bool = False) -> Any:
    """Cached wrapper for ``client.get_me()`` to reduce API calls."""
    return client.get_me(user_auth=user_auth)


def load_twitter_client(require_auth: bool = False, headless: bool = False) -> tweepy.Client:
    """Initialize a Twitter client with credentials from the environment."""
    load_dotenv(override=True)

    bearer_token = os.getenv("TWITTER_BEARER_TOKEN")
    if not bearer_token:
        console.print("[red]Missing TWITTER_BEARER_TOKEN in .env")
        sys.exit(1)

    if require_auth:
        api_key = os.getenv("TWITTER_API_KEY")
        api_secret = os.getenv("TWITTER_API_SECRET")
        client_id = os.getenv("TWITTER_CLIENT_ID")
        client_secret = os.getenv("TWITTER_CLIENT_SECRET")

        if client_id and client_secret:
            saved_token = os.getenv("TWITTER_OAUTH2_ACCESS_TOKEN")
            saved_refresh_token = os.getenv("TWITTER_OAUTH2_REFRESH_TOKEN")
            saved_expires_at_str = os.getenv("TWITTER_OAUTH2_EXPIRES_AT")

            if saved_token and saved_refresh_token and saved_expires_at_str:
                try:
                    expires_at = datetime.fromisoformat(saved_expires_at_str)
                    token_info = TokenInfo(
                        token=saved_token,
                        expires_at=expires_at,
                        refresh_token=saved_refresh_token,
                    )

                    if token_info.is_expired():
                        console.print("[yellow]Access token expired, refreshing...")
                        oauth_manager = OAuthManager.for_twitter(client_id, client_secret)
                        new_token_info, error = oauth_manager.refresh_token(saved_refresh_token)

                        if error or not new_token_info:
                            raise Exception(f"Token refresh failed: {error}")

                        save_token_to_env(
                            "TWITTER_OAUTH2_ACCESS_TOKEN",
                            new_token_info.token,
                            comment="OAuth 2.0 User Context access token (auto-refreshed)",
                        )
                        if new_token_info.refresh_token:
                            save_token_to_env(
                                "TWITTER_OAUTH2_REFRESH_TOKEN",
                                new_token_info.refresh_token,
                                comment="OAuth 2.0 refresh token",
                            )
                        if new_token_info.expires_at:
                            save_token_to_env(
                                "TWITTER_OAUTH2_EXPIRES_AT",
                                new_token_info.expires_at.isoformat(),
                                comment="OAuth 2.0 token expiration time",
                            )

                        os.environ["TWITTER_OAUTH2_ACCESS_TOKEN"] = new_token_info.token
                        if new_token_info.refresh_token:
                            os.environ["TWITTER_OAUTH2_REFRESH_TOKEN"] = (
                                new_token_info.refresh_token
                            )
                        if new_token_info.expires_at:
                            os.environ["TWITTER_OAUTH2_EXPIRES_AT"] = (
                                new_token_info.expires_at.isoformat()
                            )

                        saved_token = new_token_info.token
                        console.print("[green]Token refreshed successfully")
                    else:
                        console.print("[yellow]Using saved OAuth 2.0 access token")

                    client = tweepy.Client(saved_token, wait_on_rate_limit=True)
                    test = cached_get_me(client, user_auth=False)
                    if test.data:
                        console.print(f"[green]Successfully authenticated as @{test.data.username}")
                        return client
                except Exception as e:
                    console.print(f"[yellow]Saved token/refresh failed: {e}")
                    if headless:
                        console.print(
                            "[yellow]Headless mode — skipping interactive OAuth 2.0, falling back to OAuth 1.0a"
                        )
                    else:
                        console.print("[yellow]Starting new OAuth 2.0 flow...")
            elif saved_token:
                console.print("[yellow]Saved token exists but missing refresh token or expiry")
                if headless:
                    console.print(
                        "[yellow]Headless mode — skipping interactive OAuth 2.0, falling back to OAuth 1.0a"
                    )
                else:
                    console.print(
                        "[yellow]Starting new OAuth 2.0 flow to get complete credentials..."
                    )

            if not headless:
                try:
                    oauth2_user_handler = tweepy.OAuth2UserHandler(
                        client_id=client_id,
                        client_secret=client_secret,
                        redirect_uri="http://localhost:9876/callback",
                        scope=[
                            "tweet.read",
                            "tweet.write",
                            "users.read",
                            "follows.write",
                            "offline.access",
                        ],
                    )

                    auth_url = oauth2_user_handler.get_authorization_url()
                    console.print(
                        "[yellow]Please open this URL in your browser to authorize the application:"
                    )
                    console.print(f"[blue]{auth_url}")
                    console.print("[yellow]Waiting for authorization (timeout: 5 minutes)...")

                    try:
                        _, full_url = run_oauth_callback(port=9876, timeout=300)
                        console.print("[green]Authorization received!")
                    except TimeoutError as e:
                        console.print("[red]Error: Authorization timeout")
                        console.print(f"[red]Details: {str(e)}")
                        raise
                    except Exception as e:
                        console.print("[red]Error: Authorization failed")
                        console.print(f"[red]Details: {str(e)}")
                        raise

                    access_token = oauth2_user_handler.fetch_token(authorization_response=full_url)

                    try:
                        save_token_to_env(
                            "TWITTER_OAUTH2_ACCESS_TOKEN",
                            access_token["access_token"],
                            comment="OAuth 2.0 User Context access token",
                        )

                        if "refresh_token" in access_token:
                            save_token_to_env(
                                "TWITTER_OAUTH2_REFRESH_TOKEN",
                                access_token["refresh_token"],
                                comment="OAuth 2.0 refresh token",
                            )

                        if "expires_in" in access_token:
                            expires_at = datetime.now() + timedelta(
                                seconds=access_token["expires_in"]
                            )
                            save_token_to_env(
                                "TWITTER_OAUTH2_EXPIRES_AT",
                                expires_at.isoformat(),
                                comment="OAuth 2.0 token expiration time",
                            )

                        if "refresh_token" in access_token:
                            console.print("[yellow]Saved OAuth 2.0 tokens with refresh capability")
                        else:
                            console.print(
                                "[yellow]Warning: No refresh token received (add 'offline.access' scope)"
                            )
                    except Exception as e:
                        console.print(f"[red]Error saving token: {str(e)}")
                        sys.exit(1)

                    client = tweepy.Client(
                        access_token["access_token"],
                        wait_on_rate_limit=True,
                    )
                    test = cached_get_me(client, user_auth=False)
                    if test.data:
                        console.print(f"[green]Successfully authenticated as @{test.data.username}")
                        return client

                    console.print("[red]Could not get user info after OAuth 2.0 authentication")
                    sys.exit(1)

                except tweepy.TweepyException as e:
                    console.print(f"[red]OAuth 2.0 authentication failed: {str(e)}")
                    console.print("[yellow]Falling back to OAuth 1.0a...")

        console.print("[yellow]Debug: Using OAuth 1.0a authentication")
        auth_vars = ["TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"]
        missing_auth = [var for var in auth_vars if not os.getenv(var)]
        if missing_auth:
            console.print("[red]This operation requires user authentication.")
            console.print(f"Missing credentials: {', '.join(missing_auth)}")
            sys.exit(1)

        access_token = os.getenv("TWITTER_ACCESS_TOKEN")
        access_secret = os.getenv("TWITTER_ACCESS_SECRET")

        console.print("[yellow]Debug: Using OAuth 1.0a authentication")
        console.print(f"  API Key: {api_key[:8]}..." if api_key else "  API Key: Missing")
        console.print(f"  API Secret: {'*' * 8}..." if api_secret else "  API Secret: Missing")
        console.print(
            f"  Access Token: {access_token[:8]}..." if access_token else "  Access Token: Missing"
        )
        console.print(
            f"  Access Secret: {'*' * 8}..." if access_secret else "  Access Secret: Missing"
        )

        if not all([api_key, api_secret, access_token, access_secret]):
            console.print("[red]Error: Missing OAuth credentials")
            missing = []
            if not api_key:
                missing.append("TWITTER_API_KEY")
            if not api_secret:
                missing.append("TWITTER_API_SECRET")
            if not access_token:
                missing.append("TWITTER_ACCESS_TOKEN")
            if not access_secret:
                missing.append("TWITTER_ACCESS_SECRET")
            console.print(f"[red]Missing: {', '.join(missing)}")
            sys.exit(1)

        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
            wait_on_rate_limit=True,
        )

        console.print("[yellow]Debug: Testing OAuth credentials...")
        test = cached_get_me(client, user_auth=True)
        if test.data:
            console.print(f"[green]Successfully authenticated as @{test.data.username}")
        else:
            console.print("[red]Could not get user info after authentication")
            sys.exit(1)

        return client

    console.print("[yellow]Debug: Using bearer token authentication")
    console.print(f"  Bearer token: {bearer_token[:15]}...")

    return tweepy.Client(
        bearer_token=bearer_token,
        consumer_key=None,
        consumer_secret=None,
        access_token=None,
        access_token_secret=None,
        return_type=tweepy.Response,
        wait_on_rate_limit=True,
    )


def parse_time(timestr: str) -> datetime:
    """Parse time strings like ``24h`` or ``7d`` into datetimes."""
    if timestr.endswith("h"):
        hours = int(timestr[:-1])
        return datetime.now() - timedelta(hours=hours)
    if timestr.endswith("d"):
        days = int(timestr[:-1])
        return datetime.now() - timedelta(days=days)
    try:
        return datetime.fromisoformat(timestr)
    except ValueError:
        console.print(f"[red]Invalid time format: {timestr}")
        console.print("Use format like '24h', '7d', or ISO format")
        sys.exit(1)


def format_tweet_stats(tweet: tweepy.Tweet) -> str:
    """Format engagement stats for a tweet."""
    if not hasattr(tweet, "public_metrics"):
        return ""

    metrics = tweet.public_metrics
    stats = [
        f"💟 {metrics['like_count']}" if metrics["like_count"] > 0 else "",
        f"💬 {metrics['reply_count']}" if metrics["reply_count"] > 0 else "",
        f"🔁 {metrics['retweet_count']}" if metrics["retweet_count"] > 0 else "",
    ]
    return " ".join(stat for stat in stats if stat)


def format_tweet_time(tweet: tweepy.Tweet) -> str:
    """Format a tweet timestamp for display."""
    created_at = getattr(tweet, "created_at", None)
    if not isinstance(created_at, datetime):
        return "N/A"
    return created_at.strftime("%Y-%m-%d %H:%M")


def display_tweet(tweet: tweepy.Tweet, author_info: str | None = None) -> None:
    """Display a single tweet with consistent formatting."""
    console.print(f"[cyan]{format_tweet_time(tweet)}[/cyan]")
    if author_info:
        console.print(f"[green]From: {author_info}[/green]")
    console.print(f"[magenta]Tweet ID: {tweet.id}[/magenta]")
    console.print(f"[white]Tweet: {tweet.text}[/white]")

    stats = format_tweet_stats(tweet)
    if stats:
        console.print(f"[blue]Stats: {stats}[/blue]")
    console.print("─" * 50)


def display_tweets(tweets: tweepy.Response, username: str) -> None:
    """Display tweets in a consistent format."""
    if not tweets.data:
        console.print(f"[yellow]No tweets found for @{username}")
        return

    console.print(f"\n[bold]Recent tweets from @{username}:[/bold]\n")
    for tweet in tweets.data:
        display_tweet(tweet)
