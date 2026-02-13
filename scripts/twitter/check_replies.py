#!/usr/bin/env python3
"""Check for replies to my tweets from trusted users."""

import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from .trusted_users import is_trusted_user

# Load environment variables
load_dotenv()


def get_twitter_client():
    """Get authenticated Twitter client using OAuth 2.0."""
    from twitter.twitter import load_twitter_client

    return load_twitter_client(require_auth=True)


def get_my_user_id(client):
    """Get my user ID."""
    me = client.get_me(user_auth=False)  # Use OAuth 2.0
    if me.data:
        return me.data.id
    return None


def get_my_recent_tweets(client, user_id, max_results=10):
    """Get my recent tweets."""
    tweets = client.get_users_tweets(
        id=user_id,
        max_results=max_results,
        tweet_fields=["created_at", "public_metrics", "conversation_id"],
    )
    return tweets.data if tweets.data else []


def get_replies_to_tweet(client, tweet_id, my_user_id):
    """Get replies to a specific tweet."""
    try:
        # Search for replies to this tweet
        query = f"conversation_id:{tweet_id} -from:{my_user_id}"
        tweets = client.search_recent_tweets(
            query=query,
            max_results=100,
            tweet_fields=["created_at", "author_id", "in_reply_to_user_id"],
            expansions=["author_id"],
            user_fields=["username", "name"],
        )
        return tweets
    except Exception as e:
        print(f"Error getting replies: {e}")
        return None


def main():
    """Main function to check for replies from trusted users."""
    client = get_twitter_client()
    my_user_id = get_my_user_id(client)

    if not my_user_id:
        print("Could not get user ID")
        sys.exit(1)

    print(f"My user ID: {my_user_id}")

    # Get my recent tweets
    my_tweets = get_my_recent_tweets(client, my_user_id, max_results=5)

    if not my_tweets:
        print("No recent tweets found")
        return

    print(f"\nFound {len(my_tweets)} recent tweets")

    # Check each tweet for replies
    for tweet in my_tweets:
        print(f"\nChecking replies to tweet {tweet.id}...")

        replies = get_replies_to_tweet(client, tweet.id, my_user_id)

        if not replies or not replies.data:
            print("  No replies found")
            continue

        # Get user lookup
        user_lookup = {}
        if replies.includes and "users" in replies.includes:
            for user in replies.includes["users"]:
                user_lookup[user.id] = user

        # Check each reply
        for reply in replies.data:
            author = user_lookup.get(reply.author_id)
            if author:
                username = author.username
                is_trusted = is_trusted_user(username)
                print(f"  Reply from @{username} (trusted: {is_trusted})")
                print(f"    Text: {reply.text[:100]}...")

                if is_trusted:
                    print(f"    ⚠️ NEED TO RESPOND to trusted user @{username}")


if __name__ == "__main__":
    main()
