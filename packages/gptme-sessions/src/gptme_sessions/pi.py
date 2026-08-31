"""Validation and active-branch selection for Pi native sessions."""

from __future__ import annotations

from collections.abc import Iterable

PI_SESSION_VERSION = 3

_PI_ENTRY_TYPES = frozenset(
    {
        "message",
        "model_change",
        "thinking_level_change",
        "compaction",
        "branch_summary",
        "custom",
        "custom_message",
        "label",
        "session_info",
    }
)


class PiSessionFormatError(ValueError):
    """Raised when a Pi-looking JSONL file is not a supported native session."""


def is_pi_session_header(record: object) -> bool:
    """Return whether *record* has the identifying shape of a Pi session header.

    Version is deliberately not part of the predicate. Once a file identifies
    itself as Pi, an unsupported version must fail visibly instead of falling
    through to the gptme parser.
    """
    return (
        isinstance(record, dict)
        and record.get("type") == "session"
        and isinstance(record.get("id"), str)
        and isinstance(record.get("timestamp"), str)
        and isinstance(record.get("cwd"), str)
    )


def validate_pi_records(records: list[dict]) -> list[dict]:
    """Validate and return every record in a Pi v3 tree-JSONL session."""
    if not records or not is_pi_session_header(records[0]):
        raise PiSessionFormatError("not a Pi native session header")

    header = records[0]
    version = header.get("version")
    if version != PI_SESSION_VERSION:
        raise PiSessionFormatError(
            f"unsupported Pi session version {version!r}; expected {PI_SESSION_VERSION}"
        )

    entries = records[1:]
    if not entries:
        return records

    by_id: dict[str, dict] = {}
    roots = 0
    for index, entry in enumerate(entries, start=2):
        if not isinstance(entry, dict):
            raise PiSessionFormatError(f"Pi entry on JSONL line {index} is not an object")
        entry_type = entry.get("type")
        if entry_type not in _PI_ENTRY_TYPES:
            raise PiSessionFormatError(
                f"unsupported Pi v3 entry type {entry_type!r} on JSONL line {index}"
            )

        entry_id = entry.get("id")
        parent_id = entry.get("parentId")
        timestamp = entry.get("timestamp")
        if not isinstance(entry_id, str) or not entry_id:
            raise PiSessionFormatError(f"Pi entry on JSONL line {index} has no string id")
        if entry_id in by_id:
            raise PiSessionFormatError(f"duplicate Pi entry id {entry_id!r}")
        if parent_id is not None and not isinstance(parent_id, str):
            raise PiSessionFormatError(f"Pi entry {entry_id!r} has invalid parentId")
        if not isinstance(timestamp, str) or not timestamp:
            raise PiSessionFormatError(f"Pi entry {entry_id!r} has no timestamp")
        if parent_id is None:
            roots += 1
        elif parent_id not in by_id:
            raise PiSessionFormatError(
                f"Pi entry {entry_id!r} references missing or forward parent {parent_id!r}"
            )
        by_id[entry_id] = entry

    if roots != 1:
        raise PiSessionFormatError(f"Pi session tree has {roots} roots; expected exactly one")

    return records


def active_pi_records(records: list[dict]) -> list[dict]:
    """Validate a Pi v3 tree-JSONL session and return its active branch.

    Pi appends all branches to one file. The last tree entry is the current
    leaf, so following ``parentId`` pointers back to the root selects exactly
    the branch visible to the agent. Signal and cost accounting should use
    :func:`validate_pi_records` instead because abandoned branches still ran;
    transcripts use this active-path view.

    The returned list includes the session header followed by active entries in
    chronological tree order. Unsupported versions, print/event streams,
    unknown entry types, duplicate IDs, and broken parent links fail closed.
    """
    records = validate_pi_records(records)
    header = records[0]
    entries = records[1:]
    if not entries:
        return [header]

    by_id = {entry["id"]: entry for entry in entries}

    branch_reversed: list[dict] = []
    current = entries[-1]
    seen: set[str] = set()
    while True:
        entry_id = current["id"]
        if entry_id in seen:
            raise PiSessionFormatError(f"cycle in Pi session tree at entry {entry_id!r}")
        seen.add(entry_id)
        branch_reversed.append(current)
        parent_id = current["parentId"]
        if parent_id is None:
            break
        current = by_id[parent_id]

    return [header, *reversed(branch_reversed)]


def pi_content_text(content: object) -> str:
    """Return visible text from a Pi message content value.

    Thinking and image blocks are intentionally excluded, matching the other
    normalized transcript adapters.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    )


def pi_usage_sources(records: Iterable[dict]) -> Iterable[dict]:
    """Yield non-duplicated Pi usage objects from active records.

    ``retainedTail`` messages inside compactions are snapshots of messages
    already present earlier in the file and are therefore never traversed.
    """
    for entry in records:
        if entry.get("type") == "message":
            message = entry.get("message") or {}
            usage = message.get("usage") if isinstance(message, dict) else None
        elif entry.get("type") in ("compaction", "branch_summary"):
            usage = entry.get("usage")
        else:
            usage = None
        if isinstance(usage, dict):
            yield usage
