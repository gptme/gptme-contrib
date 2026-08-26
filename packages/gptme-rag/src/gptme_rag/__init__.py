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
        try:
            from .indexing.indexer import Indexer

            return Indexer
        except ImportError as e:
            raise AttributeError(
                f"module 'gptme_rag' has no attribute {name!r} "
                "(heavy deps not installed; install gptme-rag[full])"
            ) from e
    if name == "ContextAssembler":
        try:
            from .query.context_assembler import ContextAssembler

            return ContextAssembler
        except ImportError as e:
            raise AttributeError(
                f"module 'gptme_rag' has no attribute {name!r} "
                "(heavy deps not installed; install gptme-rag[full])"
            ) from e
    raise AttributeError(f"module 'gptme_rag' has no attribute {name!r}")


def __dir__() -> list[str]:
    return __all__
