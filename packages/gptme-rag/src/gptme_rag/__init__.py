from .lesson_matcher import (
    filter_by_session_category,
    keyword_to_regex,
    match_keyword,
    scan_lessons,
    score_lessons,
)
from .observability import (
    IndexHealth,
    InjectionStats,
    assess_index_health,
    log_injection,
    summarize_injections,
)

__all__ = [
    "ContextAssembler",
    "IndexHealth",
    "Indexer",
    "InjectionStats",
    "assess_index_health",
    "filter_by_session_category",
    "keyword_to_regex",
    "log_injection",
    "match_keyword",
    "scan_lessons",
    "score_lessons",
    "summarize_injections",
]


def __getattr__(name: str) -> object:
    if name == "Indexer":
        from .indexing.indexer import Indexer

        return Indexer
    if name == "ContextAssembler":
        from .query.context_assembler import ContextAssembler

        return ContextAssembler
    raise AttributeError(f"module 'gptme_rag' has no attribute {name!r}")
