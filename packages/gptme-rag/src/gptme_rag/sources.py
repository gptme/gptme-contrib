"""Source-descriptor document collection for gptme-rag.

Canonical retrieval needs more than a way to *query* documents — it needs a
way to *describe where documents come from* so that a consumer (an agent hook,
a CLI, a batch indexer) can collect a reproducible corpus from a declarative
list of sources, instead of hand-writing ad-hoc ``glob`` loops each time a new
source is added.

This module provides two things, ported from the Bob brain script
(``scripts/build-ambient-memory-index.py``) where the pattern was measured on
real session data:

1. **A source registry** (:class:`SourceDescriptor` + :class:`SourceRegistry`)
   — a declarative way to register document sources and collect from them.
   Each source is a zero-argument callable returning
   :class:`~gptme_rag.indexing.document.Document` objects, tagged *always-on*
   (run on every build) or *gated* (only run on full builds, e.g. when
   memory-type tagging is requested).

2. **Voice-call de-accumulation** (:func:`de_accumulate_transcript` +
   :func:`collect_voice_call_documents`) — a format-normalization utility for
   cumulative voice-call transcripts, plus a collector that turns a directory
   of archived call JSON into indexable documents.

The registry is deliberately *policy-free*: which directories are sources, and
which are always-on vs. gated, is consumer configuration.  Only the mechanism
moves upstream.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .indexing.document import Document

logger = logging.getLogger(__name__)

#: A source collector: a zero-argument callable yielding Documents.
SourceCollector = Callable[[], Iterable[Document]]


def de_accumulate_transcript(transcript: list[dict[str, Any]]) -> str:
    """Convert a cumulative transcript list to de-duplicated turn text.

    Some voice-call transcripts are *cumulative*: each entry in a same-role run
    contains all prior text for that role, so entries grow monotonically within
    a run.  Indexing such a transcript raw stores the same utterance many times
    over and weights the final turn far above earlier turns.

    Taking the **longest** text per contiguous same-role run recovers the full
    final utterance and drops the intermediate partial accumulations.

    Args:
        transcript: A list of turn dicts, each with ``role`` and ``text`` keys.

    Returns:
        A ``"ROLE: text"`` block per speaker, blank-line separated.  Empty when
        *transcript* has no non-empty turns.
    """
    turns: list[str] = []
    i = 0
    while i < len(transcript):
        if not isinstance(transcript[i], dict):
            i += 1
            continue
        current_role = str(transcript[i].get("role", ""))
        run_texts: list[str] = []
        # Collect all entries for this role run, skipping non-dict entries
        # mid-run without ending the run (P1 fix: a non-dict in the middle of
        # a same-role run used to break the inner loop and start a new run,
        # mis-treating what could be a cumulative sequence as two independent runs).
        while i < len(transcript):
            if not isinstance(transcript[i], dict):
                i += 1
                continue
            if transcript[i].get("role", "") != current_role:
                break
            run_texts.append(str(transcript[i].get("text", "")))
            i += 1
        # Detect cumulative runs: each entry is a prefix of the next (growing STT
        # partials, common in voice-call archives).  Only then is it safe to drop
        # all but the longest entry.  Independent consecutive same-role turns must
        # be joined to avoid silent data loss (P1 finding).
        def _each_is_prefix(texts: list[str]) -> bool:
            return all(
                texts[j].strip().startswith(texts[j - 1].strip())
                for j in range(1, len(texts))
            )

        is_cumulative = len(run_texts) > 1 and _each_is_prefix(run_texts)
        if is_cumulative:
            # Use stripped length so whitespace-only entries never win over
            # real content (P1 fix: raw len would pick "          " over "hello").
            best = max(run_texts, key=lambda t: len(t.strip()))
        else:
            best = "\n".join(t.strip() for t in run_texts if t.strip())
        if best.strip():
            turns.append(f"{current_role.upper()}: {best.strip()}")
    return "\n\n".join(turns)


@dataclass
class SourceDescriptor:
    """Describe a document source: a collector plus its gating policy.

    Attributes:
        name: Human-readable source name (used in logs and metadata).
        collect: Zero-argument callable returning an iterable of
            :class:`~gptme_rag.indexing.document.Document`.
        always_on: When ``True`` (default) the source is collected on every
            build.  When ``False`` it is *gated* — collected only when a full
            build is explicitly requested.
    """

    name: str
    collect: SourceCollector
    always_on: bool = True


class SourceRegistry:
    """Register document sources and collect from them in one pass.

    Usage::

        registry = SourceRegistry()
        registry.register(SourceDescriptor("voice calls", collect_voice_call_documents))
        docs = registry.collect()          # always-on sources only
        docs = registry.collect(gated=True)  # all sources
    """

    def __init__(self) -> None:
        self._sources: list[SourceDescriptor] = []

    def register(self, descriptor: SourceDescriptor) -> None:
        """Add a source descriptor to the registry."""
        self._sources.append(descriptor)

    def add(
        self,
        name: str,
        collect: SourceCollector,
        *,
        always_on: bool = True,
    ) -> None:
        """Convenience wrapper around :meth:`register`."""
        self.register(SourceDescriptor(name=name, collect=collect, always_on=always_on))

    def collect(self, *, gated: bool = False) -> list[Document]:
        """Collect documents from all eligible sources.

        Args:
            gated: When ``False`` (default), only *always-on* sources are
                collected.  When ``True``, all registered sources are collected.

        Returns:
            Flattened list of :class:`~gptme_rag.indexing.document.Document`
            objects, in registration order.
        """
        docs: list[Document] = []
        for descriptor in self._sources:
            if not gated and not descriptor.always_on:
                continue
            try:
                for doc in descriptor.collect():
                    docs.append(doc)
            except Exception:  # noqa: BLE001 — one bad source must not sink the build
                logger.exception("sources: collector %r raised", descriptor.name)
        return docs

    @property
    def sources(self) -> tuple[SourceDescriptor, ...]:
        """The registered descriptors, in registration order."""
        return tuple(self._sources)


def collect_voice_call_documents(
    voice_calls_dir: Path,
    *,
    repo_root: Path | None = None,
) -> list[Document]:
    """Collect archived voice-call transcripts as indexable documents.

    Each archived call JSON becomes one :class:`Document` keyed
    ``voicecall:<stem>``.  The content is the de-accumulated conversation
    transcript (see :func:`de_accumulate_transcript`) so that searches against
    discussion topics (e.g. "Twitter spend cap") find the call where the topic
    was discussed.

    Args:
        voice_calls_dir: Directory containing ``*.json`` call archives, each
            with a ``transcript`` list of ``{"role", "text"}`` dicts.
        repo_root: Optional repository root; when set, only calls under it are
            collected (a safety guard against indexing paths outside the repo)
            and ``metadata["source"]`` is written relative to it.

    Returns:
        List of :class:`Document` objects (possibly empty if the directory is
        missing or contains no parseable transcripts).
    """
    voice_calls_dir = Path(voice_calls_dir)
    if not voice_calls_dir.exists():
        return []
    if repo_root is not None and not voice_calls_dir.resolve().is_relative_to(
        Path(repo_root).resolve()
    ):
        return []

    docs: list[Document] = []
    for path in sorted(voice_calls_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        transcript = data.get("transcript", [])
        if not isinstance(transcript, list) or not transcript:
            continue
        text = de_accumulate_transcript(transcript)
        if not text.strip():
            continue

        stem = path.stem
        # Filename: 20260811T080814Z-358-twilio-CA...  — parse date from prefix
        date_str = ""
        if len(stem) >= 8 and stem[:8].isdigit():
            ts = stem[:8]
            date_str = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        source = str(data.get("source", "")) if isinstance(data.get("source"), str) else ""
        title = f"Voice call {date_str}" + (f" ({source})" if source else "")

        try:
            source_path = (
                path
                if repo_root is None
                else path.resolve().relative_to(Path(repo_root).resolve())
            )
        except ValueError:
            # path.resolve() points outside repo_root (e.g. a per-file symlink
            # that escapes the repo even though the directory passed the guard).
            logger.warning("sources: skipping %s — resolves outside repo_root", path)
            continue
        metadata: dict[str, Any] = {
            "type": "voicecall",
            "source": str(source_path),
            "title": title,
            "date": date_str,
        }
        docs.append(
            Document(
                content=text,
                metadata=metadata,
                source_path=path,
                doc_id=f"voicecall:{stem}",
            )
        )
    return docs


__all__ = [
    "SourceCollector",
    "SourceDescriptor",
    "SourceRegistry",
    "de_accumulate_transcript",
    "collect_voice_call_documents",
]
