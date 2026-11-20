"""Fail-closed GitHub repository identity from git remote URLs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_SLUG_RE = re.compile(r"([\w.-]+/[\w.-]+?)(?:\.git)?/?", re.ASCII)
_SCP_RE = re.compile(r"(?:git@)?github\.com:([^\s]+)")


def github_remote_slug(remote: str) -> str | None:
    """Accept only GitHub's exact host, never a hostname suffix or local path."""
    if "://" in remote:
        try:
            url = urlsplit(remote)
            if (
                url.scheme not in ("https", "http", "ssh", "git")
                or url.hostname != "github.com"
                or url.port
                not in (None, {"https": 443, "http": 80, "ssh": 22, "git": 9418}[url.scheme])
                or url.query
                or url.fragment
            ):
                return None
            path = url.path.removeprefix("/")
        except ValueError:
            return None
    else:
        match = _SCP_RE.fullmatch(remote)
        if not match:
            return None
        path = match.group(1)
    match = _SLUG_RE.fullmatch(path)
    return match.group(1) if match else None
