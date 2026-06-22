"""Shared retrieval helpers for CC memory scoring and state management.

This module provides the scoring engine that evaluates which memories are
relevant to the current session context, and manages persistent retrieval
state (confidence, recency, injection count).
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gptme_cc_memory.schema import MemoryFile, discover_memory_files

# Stopwords for lexical scoring
STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "get",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "them",
        "this",
        "to",
        "use",
        "when",
        "with",
        "you",
        "your",
    }
)

# Scoring constants
RECENCY_FLOOR = 0.18
RECENCY_HALF_LIFE_DAYS = 45.0
MIN_MATCH_OVERLAP = 2
MIN_RELEVANCE_SCORE = 0.85


def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, strip punctuation."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenize(text: str) -> list[str]:
    """Tokenize text into non-stopword tokens."""
    normalized = _normalize(text)
    return [t for t in normalized.split() if t and t not in STOPWORDS]


def _coerce_timestamp(raw: Any, fallback: datetime) -> datetime:
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return fallback
    return fallback


def _last_verified(
    entry: MemoryFile,
    state: dict[str, dict[str, Any]],
) -> datetime:
    fallback = datetime.fromtimestamp(entry.path.stat().st_mtime, tz=timezone.utc)
    return _coerce_timestamp(state.get(entry.name, {}).get("last_verified"), fallback)


def _confidence(
    entry: MemoryFile,
    state: dict[str, dict[str, Any]],
) -> float:
    try:
        confidence = float(state.get(entry.name, {}).get("confidence", entry.default_confidence))
    except (TypeError, ValueError):
        confidence = entry.default_confidence
    return max(0.0, min(confidence, 1.0))


def _recency_weight(last_verified: datetime, now: datetime) -> float:
    age_days = max(0.0, (now - last_verified).total_seconds() / 86400.0)
    weight = math.exp(-math.log(2.0) * (age_days / RECENCY_HALF_LIFE_DAYS))
    return max(RECENCY_FLOOR, round(weight, 3))


def _lexical_score(
    query: str,
    query_tokens: set[str],
    entry: MemoryFile,
) -> tuple[float, list[str]]:
    """Compute lexical match score between query and memory entry."""
    normalized_query = _normalize(query)
    entry_tokens = set(_tokenize(f"{entry.description} {entry.body} {' '.join(entry.aliases)}"))
    overlap = sorted(query_tokens & entry_tokens)
    score = 0.0

    for alias in entry.aliases:
        alias_norm = _normalize(alias)
        alias_tokens = [t for t in alias_norm.split() if t not in STOPWORDS]
        if alias_norm and alias_norm in normalized_query and len(alias_tokens) >= 2:
            score += 2.5
            overlap = sorted(set(overlap) | set(alias_tokens))
            break

    if len(overlap) < MIN_MATCH_OVERLAP and score == 0.0:
        return 0.0, []

    if query_tokens:
        score += 3.0 * (len(overlap) / min(len(query_tokens), 6))
    return score, overlap[:5]


def load_memory_state(state_file: Path) -> dict[str, dict[str, Any]]:
    """Load persistent memory retrieval state from JSON."""
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_memory_state(state: dict[str, dict[str, Any]], state_file: Path) -> None:
    """Save persistent memory retrieval state to JSON."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def select_relevant_memories(
    prompt: str,
    *,
    memory_dir: Path,
    state_file: Path,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Score all memory files against a prompt and return top-N relevant entries.

    Scoring formula::

        score = lexical_match(prompt, memory_body) × confidence × recency × type_boost

    Args:
        prompt: The current user prompt or session context.
        memory_dir: Directory containing memory markdown files.
        state_file: Path to metadata.json for persistent state.
        limit: Maximum number of entries to return.

    Returns:
        List of scored memory entries, sorted by relevance descending.
    """
    query = prompt.strip()
    query_tokens = set(_tokenize(query))
    if not query or len(query_tokens) < MIN_MATCH_OVERLAP:
        return []

    state = load_memory_state(state_file)
    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []

    for entry in discover_memory_files(memory_dir):
        lexical, matched_terms = _lexical_score(query, query_tokens, entry)
        if lexical <= 0:
            continue

        confidence = _confidence(entry, state)
        last_verified = _last_verified(entry, state)
        recency = _recency_weight(last_verified, now)
        score = lexical * confidence * recency * entry.type_boost
        if score < MIN_RELEVANCE_SCORE:
            continue

        results.append(
            {
                "key": entry.name,
                "name": entry.name,
                "type": entry.type,
                "description": entry.description,
                "excerpt": entry.excerpt,
                "score": round(score, 3),
                "confidence": round(confidence, 2),
                "recency": recency,
                "matched_terms": matched_terms,
            }
        )

    results.sort(key=lambda item: (-item["score"], -item["confidence"], item["name"]))
    return results[:limit]


def render_relevant_memory_block(entries: list[dict[str, Any]]) -> str:
    """Render scored memory entries as a context block for prompt injection."""
    if not entries:
        return ""

    lines = [
        "<memory_relevant_entries>",
        "## Relevant Durable Memory",
    ]
    for entry in entries:
        lines.append(
            f"- [{entry['type']}] {entry['name']} "
            f"(confidence {entry['confidence']:.2f}, recency {entry['recency']:.2f})"
        )
        if entry["matched_terms"]:
            lines.append(f"  Match: {', '.join(entry['matched_terms'])}")
        if entry["excerpt"]:
            lines.append(f"  {entry['excerpt']}")
    lines.append("</memory_relevant_entries>")
    return "\n".join(lines)


def record_memory_injections(
    keys: list[str],
    *,
    state_file: Path,
) -> None:
    """Record that specific memory files were injected into a session."""
    if not keys:
        return
    state = load_memory_state(state_file)
    now = datetime.now(timezone.utc).isoformat()
    for key in keys:
        entry_state = state.get(key, {})
        entry_state["last_injected"] = now
        entry_state["injections"] = int(entry_state.get("injections", 0)) + 1
        state[key] = entry_state
    save_memory_state(state, state_file)


def _entry_referenced(
    normalized_text: str,
    text_tokens: set[str],
    entry: MemoryFile,
) -> bool:
    """Check whether a text references a memory entry by alias."""
    for alias in entry.aliases:
        alias_norm = _normalize(alias)
        alias_tokens = [t for t in alias_norm.split() if t not in STOPWORDS]
        if len(alias_tokens) >= 2 and alias_norm and alias_norm in normalized_text:
            return True
        if len(alias_tokens) >= 3:
            overlap = len(set(alias_tokens) & text_tokens)
            if overlap / len(alias_tokens) >= 0.75:
                return True
    return False


def update_memory_state_from_text(
    text: str,
    *,
    memory_dir: Path,
    state_file: Path,
) -> list[str]:
    """Update memory confidence when a text references memory entries.

    Reads the full text (e.g. a session trajectory), finds referenced memory
    files by alias, and boosts their confidence + updates last_verified.

    Returns the list of memory keys that were updated.
    """
    normalized_text = _normalize(text)
    text_tokens = set(_tokenize(text))
    if len(text_tokens) < MIN_MATCH_OVERLAP:
        return []

    state = load_memory_state(state_file)
    now = datetime.now(timezone.utc).isoformat()
    matched: list[str] = []

    for entry in discover_memory_files(memory_dir):
        if not _entry_referenced(normalized_text, text_tokens, entry):
            continue
        matched.append(entry.name)
        entry_state = state.get(entry.name, {})
        confidence = max(_confidence(entry, state), entry.default_confidence)
        entry_state["confidence"] = round(min(1.0, confidence + 0.05), 2)
        entry_state["last_verified"] = now
        entry_state["references"] = int(entry_state.get("references", 0)) + 1
        state[entry.name] = entry_state

    if matched:
        save_memory_state(state, state_file)
    return matched
