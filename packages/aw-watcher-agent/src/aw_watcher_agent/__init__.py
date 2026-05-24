"""aw-watcher-agent: log AI coding-assistant sessions to a local aw-server."""

from .client import AWClient, AWClientError, Event, DEFAULT_SERVER, utc_now_iso

__all__ = ["AWClient", "AWClientError", "Event", "DEFAULT_SERVER", "utc_now_iso"]
__version__ = "0.1.0"
