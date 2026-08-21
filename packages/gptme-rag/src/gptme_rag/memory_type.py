"""Memory-type classification and ranking boost for gptme-rag.

Documents in a personal knowledge base serve different roles: some encode
identity (SOUL.md, ABOUT.md), others encode active goals, ongoing projects, or
user preferences.  Classifying documents by *memory type* lets the ranking layer
boost results that match the caller's retrieval intent without hard-filtering.

The classification is driven by a caller-supplied rules dict (loaded from JSON)
rather than hardcoded paths — callers embed their own policy and pass it in.
This keeps the library generic while the brain repo retains its config.

Example rules dict:

    {
        "exact_paths": {"SOUL.md": "identity", "ABOUT.md": "identity"},
        "glob_paths": {"tasks/*.md": "project"},
        "task_rules": {
            "goal_priorities": ["high"],
            "goal_states": ["active"],
            "preference_tags": ["preference"],
            "project_tags": ["project"],
            "default": "project"
        }
    }

Usage::

    rules = load_memory_type_map(Path("state/ambient-memory/memory-type-map.json"))
    memory_type = classify_memory_type("tasks/my-task.md", {"priority": "high"}, rules)
    score = weighted_similarity(0.8, memory_type, {"goal", "identity"})
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Memory types the library recognises.  Others are treated as unknown and
#: silently ignored rather than causing a hard error.
SUPPORTED_MEMORY_TYPES: tuple[str, ...] = ("identity", "preference", "goal", "project")

#: Multiplicative boost applied to the similarity score when the document's
#: memory type matches the set of requested types.
MEMORY_TYPE_BOOST: float = 1.35

#: Multiplicative penalty applied when the document's memory type does *not*
#: match any of the requested types (and the caller provided a non-empty set).
MEMORY_TYPE_PENALTY: float = 0.9


# ---------------------------------------------------------------------------
# Rules loading
# ---------------------------------------------------------------------------


def load_memory_type_map(map_file: Path | None = None) -> dict[str, Any]:
    """Load memory-type classification rules from a JSON file.

    Returns an empty dict when *map_file* is ``None`` or does not exist —
    callers can safely treat a missing file as "no tagging".

    The expected file schema::

        {
            "exact_paths":  {"rel/path.md": "identity"},
            "glob_paths":   {"tasks/*.md": "project"},
            "task_rules":   {
                "goal_priorities":  ["high"],
                "goal_states":      ["active"],
                "preference_tags":  ["preference"],
                "project_tags":     ["project"],
                "default":          "project"
            }
        }

    Args:
        map_file: Absolute or repo-relative path to the JSON rules file.
    """
    if map_file is None or not map_file.exists():
        return {}
    try:
        data = json.loads(map_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Could not load memory-type map from %s: %s", map_file, exc)
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Glob helper
# ---------------------------------------------------------------------------


def _glob_match_path(path: str, pattern: str) -> bool:
    """Match *path* against *pattern*, treating ``**`` as a recursive wildcard.

    Unlike :func:`fnmatch.fnmatch`, this function supports ``**`` to mean
    "zero or more path segments" (standard shell glob / gitignore semantics).
    ``*`` still matches within a single directory segment only.

    Examples::

        _glob_match_path("knowledge/design.md", "knowledge/**/*.md")  # True
        _glob_match_path("knowledge/tech/design.md", "knowledge/**/*.md")  # True
        _glob_match_path("people/alice.md", "people/*.md")  # True
    """
    # Build a regex from the pattern character-by-character.
    regex_parts: list[str] = []
    i = 0
    while i < len(pattern):
        chunk = pattern[i:]
        if chunk.startswith("**/"):
            regex_parts.append("(.*/)?")  # zero or more directories
            i += 3
        elif chunk.startswith("**"):
            regex_parts.append(".*")  # match anything including /
            i += 2
        elif pattern[i] == "*":
            regex_parts.append("[^/]*")  # single-directory wildcard
            i += 1
        elif pattern[i] == "?":
            regex_parts.append("[^/]")
            i += 1
        else:
            regex_parts.append(re.escape(pattern[i]))
            i += 1
    regex = "".join(regex_parts)
    return bool(re.fullmatch(regex, path))


# ---------------------------------------------------------------------------
# Frontmatter helpers (inline — avoids cross-module dep on task_retrieval)
# ---------------------------------------------------------------------------


def _extract_frontmatter(content: str) -> dict[str, Any]:
    """Parse a YAML frontmatter block if present; returns {} on failure."""
    content = content.replace("\r\n", "\n")
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---\n", 4)
    if end == -1:
        # Also accept frontmatter whose closing --- has no trailing newline
        # (common when editors omit the final newline on save).
        if content.endswith("\n---"):
            end = len(content) - 4
        else:
            return {}
    frontmatter_text = content[4:end]
    try:
        import yaml  # optional — yaml is available in gptme-rag's dependency set

        parsed = yaml.safe_load(frontmatter_text) or {}
    except Exception:
        # Fallback: simple key: value regex (no nested structures). Inline YAML
        # lists stay as strings here and are normalised by _coerce_string_list.
        parsed = {k: v for k, v in re.findall(r"^(\w[\w_-]*):\s*(.*)", frontmatter_text, re.M)}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_string_list(value: Any) -> list[str]:
    """Normalise a frontmatter value to a list of lowercase strings.

    Handles both list values and plain strings, including comma-separated
    strings that YAML leaves as a single string (e.g. ``tags: ai, project``).
    """
    if isinstance(value, str):
        # Split on commas to handle both plain comma-separated tags and inline
        # YAML lists emitted as strings by the no-PyYAML fallback parser.
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        return [p.strip().lower() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        # Also split each list item on commas — YAML may parse "- preference, project"
        # as a single list element containing a comma-separated string.
        return [
            p.strip().lower()
            for item in value
            if isinstance(item, str)
            for p in item.split(",")
            if p.strip()
        ]
    return []


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_memory_type(
    rel_path: str,
    metadata: dict[str, Any] | None,
    rules: dict[str, Any],
) -> str | None:
    """Resolve a document's memory type from classification rules + metadata.

    Resolution order:
    1. ``exact_paths`` — precise path match (highest priority)
    2. ``glob_paths`` — fnmatch pattern match
    3. ``task_rules`` — for ``tasks/`` prefixed paths, inspect metadata fields

    Returns one of :data:`SUPPORTED_MEMORY_TYPES` or ``None`` when no rule
    matches.

    Args:
        rel_path: Repository-relative path to the document (e.g. ``"tasks/foo.md"``).
        metadata: Frontmatter metadata dict, if available.  May be ``None``.
        rules: Classification rules dict as returned by :func:`load_memory_type_map`.
    """
    metadata = metadata or {}

    # 1. Exact-path match
    exact_paths = rules.get("exact_paths", {})
    if isinstance(exact_paths, dict) and rel_path in exact_paths:
        memory_type = str(exact_paths[rel_path])
        if memory_type in SUPPORTED_MEMORY_TYPES:
            return memory_type
        # Unsupported type (e.g. typo) — fall through to glob/task rules

    # 2. Glob-pattern match
    glob_paths = rules.get("glob_paths", {})
    if isinstance(glob_paths, dict):
        for pattern, memory_type in glob_paths.items():
            memory_type_str = str(memory_type)
            if _glob_match_path(rel_path, pattern) and memory_type_str in SUPPORTED_MEMORY_TYPES:
                return memory_type_str

    # 3. Task-specific rules (only for paths under tasks/)
    if rel_path.startswith("tasks/"):
        task_rules = rules.get("task_rules", {})
        if not isinstance(task_rules, dict):
            return None
        priority = str(metadata.get("priority", "")).strip().lower()
        state = str(metadata.get("state", "")).strip().lower()
        tags = _coerce_string_list(metadata.get("tags"))
        goal_priorities = _coerce_string_list(task_rules.get("goal_priorities"))
        goal_states = _coerce_string_list(task_rules.get("goal_states"))
        preference_tags = _coerce_string_list(task_rules.get("preference_tags"))
        project_tags = _coerce_string_list(task_rules.get("project_tags"))

        if priority in goal_priorities:
            return "goal"
        if state in goal_states:
            return "goal"
        if any(tag in preference_tags for tag in tags):
            return "preference"
        if any(tag in project_tags for tag in tags):
            return "project"
        default_type = task_rules.get("default")
        return default_type if default_type in SUPPORTED_MEMORY_TYPES else None

    return None


def classify_document(
    rel_path: str,
    content: str,
    rules: dict[str, Any],
) -> str | None:
    """Classify a document by memory type, extracting frontmatter automatically.

    Convenience wrapper over :func:`classify_memory_type` that parses the
    document's YAML frontmatter from *content* so callers do not need to parse
    it themselves.

    Args:
        rel_path: Repository-relative path to the document.
        content: Raw document content (may include frontmatter).
        rules: Classification rules dict.
    """
    metadata = _extract_frontmatter(content)
    return classify_memory_type(rel_path, metadata, rules)


# ---------------------------------------------------------------------------
# Ranking boost
# ---------------------------------------------------------------------------


def weighted_similarity(
    similarity: float,
    memory_type: str | None,
    requested_memory_types: set[str] | None,
) -> float:
    """Apply a boost or penalty to *similarity* based on memory-type match.

    When the caller has no preference (``requested_memory_types`` is ``None`` or
    empty), the similarity is returned unchanged.  When a preference is provided:

    * A match applies :data:`MEMORY_TYPE_BOOST` (default 1.35×), clamped to 1.0.
    * A miss applies :data:`MEMORY_TYPE_PENALTY` (default 0.9×).

    Args:
        similarity: Raw cosine similarity score in ``[0, 1]``.
        memory_type: The document's resolved memory type (or ``None``).
        requested_memory_types: Set of types the caller wants to boost.
    """
    if not requested_memory_types or not memory_type:
        return similarity
    if memory_type in requested_memory_types:
        return min(1.0, similarity * MEMORY_TYPE_BOOST)
    return similarity * MEMORY_TYPE_PENALTY
