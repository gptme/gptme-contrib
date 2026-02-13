"""Trusted users for Twitter auto-post functionality.

Trusted users are allowed to have their replies auto-posted without manual approval.
This enables faster response times for known collaborators.
"""

# Trusted users who can have auto-posted replies
TRUSTED_USERS = {
    "ErikBjare",  # Creator
    "Alice",  # Collaborator agent
    # Add more as needed
}


def is_trusted_user(username: str) -> bool:
    """Check if a user is in the trusted users list.

    Args:
        username: Twitter username (without @)

    Returns:
        True if user is trusted, False otherwise
    """
    # Case-insensitive comparison for Twitter usernames
    return username.lower() in {u.lower() for u in TRUSTED_USERS}
