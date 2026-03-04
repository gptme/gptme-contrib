"""gptme-sessions — session tracking and analytics for gptme agents.

Provides append-only JSONL-based session records with query, stats,
and analytics capabilities. Designed for any gptme agent to track
operational metadata across sessions.

Usage:
    from gptme_sessions import SessionRecord, SessionStore

    store = SessionStore(Path("state/sessions"))
    store.append(SessionRecord(harness="claude-code", model="opus", outcome="productive"))
    stats = store.stats()
"""

from .record import MODEL_ALIASES, SessionRecord
from .store import SessionStore

__all__ = ["SessionRecord", "SessionStore", "MODEL_ALIASES"]
