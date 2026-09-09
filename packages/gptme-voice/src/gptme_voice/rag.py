"""Fast gptme-rag lookup for live voice sessions.

The 2026-09-09 standup "what have you been doing in the last hour?" lookup
was dispatched through the fast subagent and killed at 30s (GNU ``timeout``
in ``tool_bridge``, rc=124). Cold ``gptme-rag search`` against the default
index is also the wrong tool for that query: it took ~60s here and ranked
``ABOUT.md`` above today's journals.

This module exposes a synchronous-feeling realtime function that searches a
*recency-scoped* journal corpus through gptme-rag (lexical TF-IDF first,
dense Indexer fallback) and returns in well under the voice tool budget.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

TOOL_NAME = "workspace_search"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_RECENCY_HOURS = 24.0
_MAX_RESULTS = 5
_MAX_SNIPPET = 400
_MAX_FILES = 80
_MAX_FILE_BYTES = 200_000

# Recency phrasing from the 2026-09-09 standup and typical follow-ups.
_RECENCY_RE = re.compile(
    r"\b("
    r"last hour|past hour|this hour|the last hour|"
    r"today|this morning|this afternoon|"
    r"recent(?:ly)?|lately|"
    r"what have you been (?:doing|working on)|"
    r"what (?:did|have) you (?:do|done|been doing)"
    r")\b",
    re.IGNORECASE,
)
_STOP_RE = re.compile(
    r"\b(what|have|has|you|been|doing|working|on|in|the|a|an|of|for|"
    r"please|like|just|specifically|bob)\b",
    re.IGNORECASE,
)

_TRUE = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value <= 0:
        return default
    return value


def rag_tool_schema() -> dict[str, Any]:
    """Realtime function schema for a low-latency workspace lookup."""
    return {
        "type": "function",
        "name": TOOL_NAME,
        "description": (
            "Fast local retrieval over recent journals and session notes via "
            "gptme-rag. Use this for recap questions such as 'what have you "
            "been doing in the last hour?', 'what did you work on today?', or "
            "any recent-activity lookup. Returns in a few seconds. Prefer this "
            "over the subagent for recap queries — the subagent timed out on "
            "exactly this question. Do not use it for hangup, handoff, or "
            "broad investigations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language lookup, e.g. 'what has Bob been "
                        "doing in the last hour'."
                    ),
                },
                "n_results": {
                    "type": "integer",
                    "description": "Maximum snippets to return (default 5, cap 8).",
                },
            },
            "required": ["query"],
        },
    }


def rag_instruction_preamble() -> str:
    """Session-instruction block prepended when the tool is advertised."""
    return (
        "WORKSPACE SEARCH:\n"
        "- For recap questions ('what have you been doing', 'last hour', "
        "'today', 'recent work'), call workspace_search. Do NOT use the "
        "subagent for these — it times out on open-ended recap lookups.\n"
        "- Summarise the returned snippets conversationally. Mention the "
        "journal timestamps when they help.\n"
        "- Use the subagent only if workspace_search returns empty or the "
        "caller asks for a specific file, PR, or task the snippets do not "
        "cover.\n\n"
    )


def topic_terms(query: str) -> str:
    """Strip recency/stop phrasing so lexical search has real terms left.

    'what has Bob been doing in the last hour' → '' (pure recency).
    'training run in the last hour' → 'training run'.
    """
    stripped = _RECENCY_RE.sub(" ", query)
    stripped = _STOP_RE.sub(" ", stripped)
    return " ".join(stripped.split())


def is_recency_query(query: str) -> bool:
    return bool(_RECENCY_RE.search(query))


class VoiceRag:
    """Recency-scoped gptme-rag search used by the live voice tool surface."""

    def __init__(
        self,
        workspace: str | None,
        *,
        enabled: bool | None = None,
        timeout_seconds: float | None = None,
        recency_hours: float | None = None,
        search_impl: Callable[[str, int], list[dict[str, Any]]] | None = None,
    ):
        self.workspace = Path(workspace).expanduser() if workspace else None
        self.enabled = _env_flag("GPTME_VOICE_RAG") if enabled is None else enabled
        self.timeout_seconds = (
            _env_float("GPTME_VOICE_RAG_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
            if timeout_seconds is None
            else timeout_seconds
        )
        self.recency_hours = (
            _env_float("GPTME_VOICE_RAG_RECENCY_HOURS", DEFAULT_RECENCY_HOURS)
            if recency_hours is None
            else recency_hours
        )
        self._search_impl = search_impl

    @classmethod
    def from_env(cls, workspace: str | None) -> VoiceRag:
        return cls(workspace)

    def collect_recent_files(self, now: float | None = None) -> list[Path]:
        """Newest-first journal files inside the recency window."""
        if self.workspace is None:
            return []
        journal = self.workspace / "journal"
        if not journal.is_dir():
            return []

        clock = now if now is not None else time.time()
        cutoff = clock - self.recency_hours * 3600
        today = datetime.fromtimestamp(clock, tz=timezone.utc).date()
        day_dirs = [
            journal / today.isoformat(),
            journal / (today - timedelta(days=1)).isoformat(),
        ]

        found: list[tuple[float, Path]] = []
        seen: set[Path] = set()
        for day_dir in day_dirs:
            if not day_dir.is_dir():
                continue
            for path in day_dir.glob("*.md"):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size > _MAX_FILE_BYTES:
                    continue
                # Apply the mtime cutoff to all directories including today's.
                # A journal file written hours ago in today's dir is stale for
                # sub-24h windows (e.g. GPTME_VOICE_RAG_RECENCY_HOURS=1).
                if stat.st_mtime < cutoff:
                    continue
                seen.add(resolved)
                found.append((stat.st_mtime, path))

        found.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path in found[:_MAX_FILES]]

    def _load_documents(self, paths: list[Path]) -> list[Any]:
        try:
            from gptme_rag.indexing.document import Document
        except ImportError:
            logger.warning(
                "gptme-rag is not installed; workspace_search cannot load documents"
            )
            return []

        docs = []
        for path in paths:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("skip unreadable %s: %s", path, exc)
                continue
            if not content.strip():
                continue
            rel = self._relpath(path)
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            docs.append(
                Document(
                    content=content,
                    metadata={
                        "source": rel,
                        "filename": path.name,
                        "last_modified": mtime.isoformat(),
                    },
                    source_path=path,
                    doc_id=rel,
                    last_modified=mtime,
                )
            )
        return docs

    def _relpath(self, path: Path) -> str:
        if self.workspace is None:
            return str(path)
        try:
            return str(path.resolve().relative_to(self.workspace.resolve()))
        except ValueError:
            return str(path)

    def _search_lexical(self, query: str, docs: list[Any], n_results: int) -> list[Any]:
        from gptme_rag.lexical import TfidfIndex

        index = TfidfIndex(
            stop_words="english", ngram_range=(1, 2), relevance_floor=0.0
        )
        index.index(docs)
        return [hit.document for hit in index.search(query, n_results=n_results)]

    def _search_sync(self, query: str, n_results: int) -> dict[str, Any]:
        n_results = max(1, min(int(n_results), 8))
        if self._search_impl is not None:
            results = self._search_impl(query, n_results)
            return {
                "status": "ok" if results else "empty",
                "query": query,
                "backend": "injected",
                "results": results[:n_results],
            }

        paths = self.collect_recent_files()
        if not paths:
            return {
                "status": "empty",
                "query": query,
                "backend": "none",
                "results": [],
                "message": (
                    "No recent journal files found. Try a more specific query "
                    "or the subagent."
                ),
            }

        docs = self._load_documents(paths)
        if not docs:
            return {
                "status": "empty",
                "query": query,
                "backend": "none",
                "results": [],
                "message": "Recent journals could not be read.",
            }

        terms = topic_terms(query)
        backend = "recency"
        ranked = list(docs)
        if terms:
            try:
                ranked = (
                    self._search_lexical(terms, docs, n_results=len(docs)) or ranked
                )
                backend = "lexical"
            except Exception as exc:  # noqa: BLE001 - lexical is optional
                logger.info("lexical search unavailable (%s); using recency order", exc)
                backend = "recency"

        # Recency queries keep last-hour files at the front even after lexical rank.
        if is_recency_query(query):
            hour_cutoff = time.time() - 3600

            def _recency_bucket(doc: Any) -> int:
                path = getattr(doc, "source_path", None)
                if path is None:
                    return 1
                try:
                    return 0 if path.stat().st_mtime >= hour_cutoff else 1
                except OSError:
                    return 1

            ranked.sort(key=_recency_bucket)

        results = [_format_result(doc) for doc in ranked[:n_results]]
        payload: dict[str, Any] = {
            "status": "ok" if results else "empty",
            "query": query,
            "backend": backend,
            "results": results,
        }
        if not results:
            payload["message"] = (
                "No matches in recent journals. Try a more specific query or the subagent."
            )
        return payload

    async def search(self, query: str, n_results: int = _MAX_RESULTS) -> dict[str, Any]:
        if not self.enabled:
            return {"error": "workspace_search is not enabled on this server."}
        if not query.strip():
            return {"error": "No query provided"}

        started = time.perf_counter()
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(self._search_sync, query.strip(), n_results),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "query": query,
                "error": (
                    f"workspace_search exceeded {self.timeout_seconds:.0f}s. "
                    "Try a narrower query."
                ),
            }
        except Exception as exc:  # noqa: BLE001
            # Any unhandled exception from _search_sync must NOT propagate out of
            # search() into the realtime client's _receive_loop, which has no
            # general handler and would terminate the WebSocket on a single bad call.
            logger.error("workspace_search error: %s", exc, exc_info=True)
            return {
                "status": "error",
                "query": query,
                "error": str(exc),
            }
        payload["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return payload


def _format_result(doc: Any) -> dict[str, Any]:
    source = ""
    if getattr(doc, "metadata", None):
        source = str(doc.metadata.get("source") or "")
    if not source and getattr(doc, "source_path", None) is not None:
        source = str(doc.source_path)
    content = (getattr(doc, "content", None) or "").strip()
    snippet = content[:_MAX_SNIPPET]
    if len(content) > _MAX_SNIPPET:
        snippet = snippet.rstrip() + "…"
    mtime = None
    last_modified = getattr(doc, "last_modified", None)
    if last_modified is not None:
        mtime = last_modified.isoformat()
    elif getattr(doc, "metadata", None):
        mtime = doc.metadata.get("last_modified")
    return {
        "source": source,
        "mtime": mtime,
        "snippet": snippet,
    }
