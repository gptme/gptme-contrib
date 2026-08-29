"""Knowledge-entry source for gptme-rag.

Reads the JSONL store written by ``gptme-util knowledge save``
(``~/.local/share/gptme/knowledge/entries.jsonl``, respects ``XDG_DATA_HOME``)
and yields :class:`~gptme_rag.indexing.document.Document` objects tagged
``memory_type="knowledge_entry"``.

This module is a *rebuildable index* over that file. It does not write the
store, score queries, or replace the JSONL as source of truth. Schema and
skip-invalid-line policy match ``gptme.knowledge`` (gptme/gptme#3622).

Chroma metadata values must be scalars (str/int/float/bool). Tags and
keywords are therefore stored as comma-separated strings; the original lists
are also folded into ``content`` so lexical search still sees them.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .indexing.document import Document

logger = logging.getLogger(__name__)


def default_knowledge_entries_path() -> Path:
    """Return the JSONL path used by gptme's knowledge store.

    Matches ``gptme.dirs.get_data_dir() / "knowledge" / "entries.jsonl"`` for
    the common cases (``XDG_DATA_HOME`` override, then ``~/.local/share/gptme``)
    without importing gptme.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "gptme" / "knowledge" / "entries.jsonl"
    return Path.home() / ".local" / "share" / "gptme" / "knowledge" / "entries.jsonl"


def _is_valid_entry(parsed: object) -> bool:
    """Return whether parsed JSON has the fields required by the store."""
    if not isinstance(parsed, dict):
        return False
    try:
        uuid.UUID(str(parsed.get("id", "")))
    except (AttributeError, TypeError, ValueError):
        return False
    if not all(
        isinstance(parsed.get(key), expected_type)
        for key, expected_type in (
            ("problem", str),
            ("resolution", str),
            ("tags", list),
            ("created_at", str),
        )
    ):
        return False
    problem = parsed["problem"].strip()
    resolution = parsed["resolution"].strip()
    if not problem or not resolution:
        return False
    tags = parsed["tags"]
    if not all(isinstance(tag, str) for tag in tags):
        return False
    # Optional field; a bare string iterates as characters and corrupts metadata.
    if "keywords" in parsed:
        keywords = parsed["keywords"]
        if not isinstance(keywords, list) or not all(
            isinstance(keyword, str) for keyword in keywords
        ):
            return False
    return True


def _join_strings(values: Iterable[object]) -> str:
    return ",".join(str(v).strip() for v in values if str(v).strip())


def _entry_content(entry: dict[str, Any]) -> str:
    tags = [str(t).strip() for t in entry.get("tags", []) if str(t).strip()]
    keywords = [str(k).strip() for k in entry.get("keywords", []) if str(k).strip()]
    parts = [
        f"Problem: {entry['problem'].strip()}",
        "",
        f"Resolution: {entry['resolution'].strip()}",
    ]
    if tags:
        parts.extend(["", f"Tags: {', '.join(tags)}"])
    if keywords:
        parts.extend(["", f"Keywords: {', '.join(keywords)}"])
    return "\n".join(parts)


def _entry_last_modified(created_at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _document_from_entry(entry: dict[str, Any], source_path: Path) -> Document:
    entry_id = str(entry["id"])
    tags = [str(t).strip() for t in entry.get("tags", []) if str(t).strip()]
    keywords = [str(k).strip() for k in entry.get("keywords", []) if str(k).strip()]
    created_at = str(entry["created_at"])
    metadata: dict[str, Any] = {
        "type": "knowledge_entry",
        "memory_type": "knowledge_entry",
        "source": str(source_path),
        "title": entry["problem"].strip(),
        "problem": entry["problem"].strip(),
        "resolution": entry["resolution"].strip(),
        "tags": _join_strings(tags),
        "keywords": _join_strings(keywords),
        "created_at": created_at,
        "id": entry_id,
    }
    return Document(
        content=_entry_content(entry),
        metadata=metadata,
        source_path=source_path,
        doc_id=f"knowledge_entry:{entry_id}",
        last_modified=_entry_last_modified(created_at),
    )


def collect_knowledge_entry_documents(
    entries_path: Path | None = None,
) -> list[Document]:
    """Collect knowledge-entry documents from a JSONL store.

    Args:
        entries_path: Path to ``entries.jsonl``. When omitted, uses
            :func:`default_knowledge_entries_path`.

    Returns:
        One :class:`Document` per valid entry. Missing files, malformed
        lines, and schema-invalid objects are skipped.
    """
    path = Path(entries_path) if entries_path is not None else default_knowledge_entries_path()
    if not path.exists() or not path.is_file():
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("knowledge_source: cannot read %s", path)
        return []

    docs: list[Document] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _is_valid_entry(parsed):
            continue
        docs.append(_document_from_entry(parsed, path))
    return docs


@dataclass
class KnowledgeEntrySource:
    """Source descriptor for the gptme knowledge JSONL store.

    Usage::

        source = KnowledgeEntrySource(entries_path=path)
        docs = source.collect()
        registry.add("knowledge entries", source.collect)
    """

    entries_path: Path | None = None

    def collect(self) -> list[Document]:
        """Collect documents from :attr:`entries_path` or the default store."""
        return collect_knowledge_entry_documents(self.entries_path)


__all__ = [
    "KnowledgeEntrySource",
    "collect_knowledge_entry_documents",
    "default_knowledge_entries_path",
]
