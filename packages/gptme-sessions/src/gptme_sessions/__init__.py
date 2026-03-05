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

from .record import MODEL_ALIASES, SessionRecord, normalize_model
from .signals import (
    detect_format,
    extract_from_path,
    extract_signals,
    extract_signals_cc,
    extract_usage_cc,
    extract_usage_gptme,
    grade_signals,
    is_productive,
)
from .store import SessionStore
from .thompson_sampling import Bandit, BanditArm, BanditState, load_bandit_means

__all__ = [
    "SessionRecord",
    "SessionStore",
    "MODEL_ALIASES",
    "normalize_model",
    "detect_format",
    "extract_usage_gptme",
    "extract_from_path",
    "extract_signals",
    "extract_signals_cc",
    "extract_usage_cc",
    "grade_signals",
    "is_productive",
    "Bandit",
    "BanditArm",
    "BanditState",
    "load_bandit_means",
]
