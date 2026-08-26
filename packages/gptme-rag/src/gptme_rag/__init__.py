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

# Note: Indexer and ContextAssembler are lazy-loaded via __getattr__ below.
# They require the full install (gptme-rag[full]) with chromadb + sentence_transformers.
# ``from gptme_rag import *`` will raise AttributeError for these names when the
# heavy deps are absent; prefer explicit imports or hasattr() guards at call sites.
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

            # Cache in module namespace so subsequent accesses skip __getattr__.
            globals()["Indexer"] = Indexer
            return Indexer
        except ImportError as e:
            raise AttributeError(
                f"module 'gptme_rag' has no attribute {name!r} "
                "(heavy deps not installed; install gptme-rag[full])"
            ) from e
    if name == "ContextAssembler":
        try:
            from .query.context_assembler import ContextAssembler

            # Cache in module namespace so subsequent accesses skip __getattr__.
            globals()["ContextAssembler"] = ContextAssembler
            return ContextAssembler
        except ImportError as e:
            raise AttributeError(
                f"module 'gptme_rag' has no attribute {name!r} "
                "(heavy deps not installed; install gptme-rag[full])"
            ) from e
    raise AttributeError(f"module 'gptme_rag' has no attribute {name!r}")


def __dir__() -> list[str]:
    # Include standard module attributes (from __dict__) plus lazy names from __all__.
    return sorted(set(globals().keys()) | set(__all__))
