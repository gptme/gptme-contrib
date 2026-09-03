"""gptme-sessions — session tracking and analytics for agents.

Supports trajectories from gptme, Claude Code, Codex, Copilot, Grok, and Pi.
Provides append-only JSONL-based session records with query, stats,
and analytics capabilities. Designed for any agent to track
operational metadata across sessions.

Usage:
    from gptme_sessions import SessionRecord, SessionStore

    store = SessionStore(Path("state/sessions"))
    store.append(SessionRecord(harness="claude-code", model="opus", outcome="productive"))
    stats = store.stats()
"""

from .discovery import (
    decode_cc_project_path,
    discover_cc_sessions,
    discover_codex_sessions,
    discover_copilot_sessions,
    discover_gptme_sessions,
    discover_pi_sessions,
    parse_gptme_config,
    session_date_from_path,
    session_datetime_from_path,
)
from .post_session import PostSessionResult, post_session
from .record import (
    HARM_CATEGORY_LABELS,
    HARM_CATEGORY_TAXONOMY,
    MODEL_ALIASES,
    SessionRecord,
    normalize_model,
)
from .signals import (
    detect_format,
    extract_from_path,
    extract_signals,
    extract_signals_cc,
    extract_signals_codex,
    extract_signals_copilot,
    extract_signals_pi,
    extract_usage_cc,
    extract_usage_codex,
    extract_usage_gptme,
    extract_usage_pi,
    grade_signals,
    is_productive,
)
from .classification import (
    Category,
    ClassificationResult,
    DEFAULT_CATEGORIES,
    classify_by_keywords,
    classify_by_llm,
    classify_session,
    judge_and_classify,
    normalize_category,
)
from .spans import (
    SpanAggregates,
    ToolSpan,
    extract_spans_from_cc_jsonl,
    extract_spans_from_gptme_jsonl,
)
from .harm_detect import (
    NoSearchReposError,
    batch_detect_harm_revert,
    check_precision_on_ground_truth,
    detect_harm_revert,
    extract_commit_shas,
)
from .store import SessionStore
from .thompson_sampling import Bandit, BanditArm, BanditState, load_bandit_means
from .transcript import (
    NormalizedMessage,
    SessionTranscript,
    SessionTree,
    SubagentNode,
    TRANSCRIPT_SCHEMA_VERSION,
    read_session_tree,
    read_transcript,
    subagent_record_files,
)

__all__ = [
    "Category",
    "ClassificationResult",
    "DEFAULT_CATEGORIES",
    "classify_by_keywords",
    "classify_by_llm",
    "classify_session",
    "judge_and_classify",
    "normalize_category",
    "SessionRecord",
    "SessionStore",
    "HARM_CATEGORY_LABELS",
    "HARM_CATEGORY_TAXONOMY",
    "MODEL_ALIASES",
    "normalize_model",
    "detect_format",
    "extract_usage_cc",
    "extract_usage_codex",
    "extract_usage_gptme",
    "extract_usage_pi",
    "extract_from_path",
    "extract_signals",
    "extract_signals_cc",
    "extract_signals_codex",
    "extract_signals_copilot",
    "extract_signals_pi",
    "grade_signals",
    "is_productive",
    "discover_gptme_sessions",
    "discover_cc_sessions",
    "discover_codex_sessions",
    "discover_copilot_sessions",
    "discover_pi_sessions",
    "parse_gptme_config",
    "decode_cc_project_path",
    "session_date_from_path",
    "session_datetime_from_path",
    "Bandit",
    "BanditArm",
    "BanditState",
    "load_bandit_means",
    "post_session",
    "PostSessionResult",
    "NormalizedMessage",
    "SessionTranscript",
    "SessionTree",
    "SubagentNode",
    "TRANSCRIPT_SCHEMA_VERSION",
    "read_session_tree",
    "read_transcript",
    "subagent_record_files",
    "SpanAggregates",
    "ToolSpan",
    "extract_spans_from_cc_jsonl",
    "extract_spans_from_gptme_jsonl",
    "NoSearchReposError",
    "batch_detect_harm_revert",
    "check_precision_on_ground_truth",
    "detect_harm_revert",
    "extract_commit_shas",
]
