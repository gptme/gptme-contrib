"""Lesson matching for gptme-rag: keyword/wildcard, session_categories, BM25.

Ported from the ``match-lessons.py`` Claude Code hook (Phase 2.4 of
the upstream-retrieval task). This module provides the *library* layer for
lesson matching — it scans, filters, and scores lessons without any
workspace-discovery or agent-specific configuration. The caller (a hook, CLI,
or harness adapter) supplies lesson directories and the query text.

**Scoring model** (additive):

- Keyword / wildcard match: +1.0 per matched keyword (supports ``*`` as
  ``\\w*`` wildcard, same logic as gptme's ``LessonMatcher``).
- Pattern match (full regex in ``match.patterns``): +1.0 per pattern.
- Skill-name exact / near-match: +1.5.
- Skill descriptor token overlap (name × description × tags): 0–3.3.
- BM25 semantic overlap: ``_BM25_WEIGHT × z-score`` (z-score relative to the
  per-query score distribution, not an absolute floor).

Thompson-sampling re-ranking is intentionally excluded from this module — it
belongs in the agent harnesses that accumulate per-lesson reward signal.

Public API::

    from gptme_rag.lesson_matcher import scan_lessons, score_lessons
    from gptme_rag.lesson_matcher import filter_by_session_category

    lessons = scan_lessons([Path("lessons")])
    lessons = filter_by_session_category(lessons, "code")
    results = score_lessons(lessons, user_prompt, max_results=5)
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from collections.abc import Sequence
from typing import Any

import regex

# ---------------------------------------------------------------------------
# BM25 constants (calibrated on 9,025 real injections, 2026-08-05)
# ---------------------------------------------------------------------------

#: BM25 term-frequency saturation factor.
BM25_K1: float = 1.2
#: BM25 length-normalisation factor.
BM25_B: float = 0.75
#: Additive weight for the normalised BM25 z-score contribution.
BM25_WEIGHT: float = 0.4

#: Minimum z-score over the per-query score distribution to admit a BM25 hit.
#: Raw BM25 scores scale with query length so an absolute floor would never
#: filter anything on long prompts.  A z-score gate is scale-free.
BM25_MIN_Z: float = 4.0

#: Absolute raw-score floor (≈ P30 on the 2026-08-05 calibration corpus).
#: Guards against spurious weak-overlap hits in degenerate tiny corpora.
BM25_MIN_RAW: float = 40.0

#: Fraction of the maximum-attainable z-score to use as the adaptive floor.
#: When fewer than 17 lessons score nonzero the theoretical z ceiling drops
#: below 4.0; this fraction keeps the gate reachable for small corpora.
BM25_STANDOUT_FRACTION: float = 0.8

#: Maximum wall-clock time for one user-supplied ``match.patterns`` search.
PATTERN_TIMEOUT_SECONDS: float = 0.01

# ---------------------------------------------------------------------------
# Internal helpers for frontmatter parsing
# ---------------------------------------------------------------------------

_SKIP_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", ".git", "node_modules", ".venv", "venv", "env"}
)


def _dedupe_strings(values: Sequence[object]) -> list[str]:
    """Strip and deduplicate strings while preserving first-seen order."""
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def _string_list(value: object) -> list[str]:
    """Normalise YAML string-or-list fields to a clean list of strings."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _clean_plain_scalar(raw: str) -> str:
    """Strip surrounding quotes, then any YAML inline ``#`` comment.

    YAML only begins a comment at a ``#`` that starts the value or is preceded
    by whitespace, so ``a#b`` survives while ``a  # b`` becomes ``a``. Quoted
    scalars keep their contents verbatim (a ``#`` inside quotes is data).

    Single-quoted YAML scalars use ``''`` (doubled single quote) as the only
    escape sequence for a literal single quote (e.g. ``'it''s'`` → ``it's``).
    """
    value = raw.strip()
    if value[:1] in {"'", '"'}:
        quote = value[0]
        if quote == "'":
            # YAML single-quoted: '' is the only escape sequence for a literal '
            result: list[str] = []
            i = 1
            while i < len(value):
                ch = value[i]
                if ch == "'" and i + 1 < len(value) and value[i + 1] == "'":
                    result.append("'")
                    i += 2
                elif ch == "'":
                    return "".join(result)
                else:
                    result.append(ch)
                    i += 1
            return "".join(result).strip()
        # Double-quoted: process standard YAML backslash escape sequences.
        # 684bff52 accidentally restored the old "skip backslash, return
        # value[1:index]" scan, which left literal backslashes in the result
        # (`"He said \"hello\""` → `He said \"hello\"` instead of `He said "hello"`).
        _DQUOTE_ESCAPES: dict[str, str] = {
            '"': '"',
            "\\": "\\",
            "n": "\n",
            "t": "\t",
            "r": "\r",
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "v": "\v",
        }
        chars: list[str] = []
        i = 1
        while i < len(value):
            ch = value[i]
            if ch == "\\":
                if i + 1 < len(value):
                    chars.append(_DQUOTE_ESCAPES.get(value[i + 1], value[i + 1]))
                    i += 2
                else:
                    chars.append("\\")
                    i += 1
            elif ch == '"':
                return "".join(chars)
            else:
                chars.append(ch)
                i += 1
        return "".join(chars).strip()
    if value.startswith("#"):
        return ""
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _extract_scalar_frontmatter_field(
    fm_str: str, field: str, *, allow_indented: bool = False
) -> str | None:
    """Extract a simple YAML scalar without PyYAML (regex fallback)."""
    lines = fm_str.splitlines()
    indent = r"[ \t]*" if allow_indented else ""
    field_pattern = re.compile(rf"^{indent}{re.escape(field)}:\s*(.*)$")

    for index, line in enumerate(lines):
        match = field_pattern.match(line)
        if not match:
            continue
        raw_value = match.group(1).strip()
        if raw_value and raw_value[0] in "|>":
            block_lines: list[str] = []
            for next_line in lines[index + 1 :]:
                if next_line and not next_line.startswith((" ", "\t")):
                    break
                block_lines.append(next_line)
            if not block_lines:
                return ""
            indents = [len(bl) - len(bl.lstrip(" ")) for bl in block_lines if bl.strip()]
            trim = min(indents) if indents else 0
            normalized = [bl[trim:] if trim else bl for bl in block_lines]
            text = "\n".join(normalized).strip()
            if raw_value[0] == ">":
                return " ".join(part.strip() for part in text.splitlines() if part.strip())
            return text
        return _clean_plain_scalar(raw_value)

    return None


def _extract_list_frontmatter_field(
    fm_str: str, field: str, *, allow_indented: bool = True
) -> list[str]:
    """Extract a YAML list field from frontmatter using regex (no PyYAML).

    Handles both inline ``[val1, val2]`` / ``["val1", "val2"]`` and block
    (``- val`` / ``- "val"``) forms.  Values may be quoted or unquoted.
    Returns an empty list if the field is absent.

    When *allow_indented* is ``False`` the key must begin at column zero.
    Use this when searching ``top_level_without_match`` so that a same-named
    field nested inside another mapping (e.g. ``metadata.keywords``) is not
    accidentally hoisted to the top-level match keywords.
    """
    indent = r"[ \t]*" if allow_indented else ""
    # Inline: field: [val1, val2] or field: ["val1", "val2"].  Match the
    # closing bracket only outside quoted values so a keyword such as "a]b"
    # does not truncate the list.
    # Quoted items must allow YAML escapes: \" inside double quotes and
    # '' (doubled single quote) inside single quotes.  A naive '[^']*'
    # splits `'it''s'` into `it` + `s`.
    _quoted_item = r'"(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\''
    inline = re.search(
        rf"^{indent}{re.escape(field)}:\s*\[((?:[^\]\"']|{_quoted_item})*)\]",
        fm_str,
        re.MULTILINE | re.DOTALL,
    )
    if inline:
        items: list[str] = []
        # Tokenize respecting quotes: commas inside "..." or '...' are not
        # separators.  Unquoted items run until the next comma or end-of-string.
        # Quoted tokens keep their quotes so _clean_plain_scalar can unescape.
        for m in re.finditer(
            rf"{_quoted_item}|([^,\'\"]+?)(?=\s*,|\s*$)",
            inline.group(1),
        ):
            raw = m.group(0)
            val = _clean_plain_scalar(raw)
            if val:
                items.append(val)
        return items

    # Block: "field:" on its own line, then more-indented "- val" lines.
    block_start = re.search(
        rf"^(?P<indent>{indent}){re.escape(field)}:\s*$",
        fm_str,
        re.MULTILINE,
    )
    if block_start:
        field_indent = len(block_start.group("indent").expandtabs())
        items = []
        for line in fm_str[block_start.end() :].splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            item_m = re.match(r"^\s*-\s+(.+?)\s*$", line)
            if item_m is None:
                # Any non-item line ends the sequence: a sibling mapping key
                # (``keywords:`` after ``session_categories:``) is indented just
                # like its parent's other keys, so indentation alone cannot tell
                # them apart.
                break
            line_indent = len(line.expandtabs()) - len(line.expandtabs().lstrip())
            if line_indent < field_indent:
                # A dedented item belongs to an enclosing sequence, not this one.
                break
            val = _clean_plain_scalar(item_m.group(1))
            if val:
                items.append(val)
        return items

    return []


def _extract_mapping_block(fm_str: str, field: str) -> str:
    """Return a nested mapping's text, or ``""`` if it is absent."""
    start = re.search(
        rf"^(?P<indent>[ \t]*){re.escape(field)}:[ \t]*$",
        fm_str,
        re.MULTILINE,
    )
    if not start:
        return ""
    parent_indent = len(start.group("indent").expandtabs())
    block: list[str] = []
    for line in fm_str[start.end() :].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            block.append(line)
            continue
        indent = len(line.expandtabs()) - len(line.expandtabs().lstrip())
        if indent <= parent_indent:
            break
        block.append(line)
    return "\n".join(block)


def extract_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body from a markdown string.

    Tries ``yaml.safe_load`` first; falls back to a regex parser that handles
    every field consumed by :func:`scan_lessons` without requiring PyYAML.

    Returns ``(frontmatter_dict, body_string)``.
    """
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    fm_str = parts[1]
    body = parts[2].strip()

    try:
        import yaml

        fm_yaml = yaml.safe_load(fm_str)
        return (fm_yaml if isinstance(fm_yaml, dict) else {}), body
    except ImportError:
        pass
    except Exception:
        pass

    # Regex fallback
    fm: dict[str, Any] = {}
    match_block = _extract_mapping_block(fm_str, "match")
    top_level_without_match = fm_str.replace(match_block, "", 1) if match_block else fm_str

    # Build the match dict with all fields scan_lessons reads from it.  Combine
    # legacy top-level and documented nested match fields like the PyYAML path.
    match_dict: dict[str, Any] = {}
    top_level_keywords = _extract_list_frontmatter_field(
        top_level_without_match, "keywords", allow_indented=False
    )
    nested_keywords = _extract_list_frontmatter_field(match_block, "keywords")
    keywords = _dedupe_strings(
        top_level_keywords
        + (
            []
            if top_level_keywords
            else _string_list(
                _extract_scalar_frontmatter_field(top_level_without_match, "keywords")
            )
        )
        + nested_keywords
        + (
            []
            if nested_keywords
            else _string_list(
                _extract_scalar_frontmatter_field(match_block, "keywords", allow_indented=True)
            )
        )
    )
    if keywords:
        match_dict["keywords"] = keywords

    # Only the nested ``match:`` form is the documented schema; a top-level
    # ``session_categories`` key must stay ignored here so the regex fallback
    # agrees with the PyYAML path and with filter_by_session_category.  This
    # field is exclusionary — wrongly honouring it silently drops lessons.
    session_categories = _extract_list_frontmatter_field(match_block, "session_categories")
    if not session_categories:
        # Also handle scalar form: session_categories: code, infrastructure
        # (may be indented under match:); _extract_scalar_frontmatter_field only
        # handles top-level unindented fields so we use a direct regex here.
        sc_m = re.search(
            r"^\s*session_categories:\s*([^\[{\n][^\n]*)",
            match_block,
            re.MULTILINE,
        )
        if sc_m:
            val = _clean_plain_scalar(sc_m.group(1))
            if val and not val.startswith("-"):
                session_categories = [s.strip() for s in val.split(",") if s.strip()]
    if session_categories:
        match_dict["session_categories"] = session_categories

    top_level_patterns = _extract_list_frontmatter_field(
        top_level_without_match, "patterns", allow_indented=False
    )
    nested_patterns = _extract_list_frontmatter_field(match_block, "patterns")
    patterns = _dedupe_strings(
        top_level_patterns
        + (
            []
            if top_level_patterns
            else _string_list(
                _extract_scalar_frontmatter_field(top_level_without_match, "patterns")
            )
        )
        + nested_patterns
        + (
            []
            if nested_patterns
            else _string_list(
                _extract_scalar_frontmatter_field(match_block, "patterns", allow_indented=True)
            )
        )
    )
    if patterns:
        match_dict["patterns"] = patterns

    if match_dict:
        fm["match"] = match_dict

    status = _extract_scalar_frontmatter_field(fm_str, "status")
    if status is not None:
        fm["status"] = status

    for field in ("id", "name", "description", "when_to_use"):
        scalar_value = _extract_scalar_frontmatter_field(fm_str, field)
        if scalar_value is not None:
            fm[field] = scalar_value

    # metadata.harness / metadata.tags — both are consumed by scan_lessons.
    # Scope extraction to metadata so top-level keys stay ignored, matching the
    # PyYAML path.
    metadata_block = _extract_mapping_block(fm_str, "metadata")
    for field in ("harness", "tags"):
        values = _extract_list_frontmatter_field(metadata_block, field)
        if not values:
            scalar_match = re.search(
                rf"^\s*{field}:\s*([^\[{{\n][^\n]*)",
                metadata_block,
                re.MULTILINE,
            )
            if scalar_match:
                scalar_value = _clean_plain_scalar(scalar_match.group(1))
                if scalar_value and not scalar_value.startswith("-"):
                    values = [item.strip() for item in scalar_value.split(",") if item.strip()]
        if values:
            fm.setdefault("metadata", {})[field] = values

    return fm, body


# ---------------------------------------------------------------------------
# Keyword / wildcard matching
# ---------------------------------------------------------------------------


def keyword_to_regex(keyword: str) -> re.Pattern[str] | None:
    """Convert a keyword (possibly with ``*`` wildcards) to a compiled regex.

    Mirrors gptme's internal ``_keyword_to_pattern``:

    * ``*`` → ``\\w*`` (zero or more word characters).
    * A keyword made only of ``*`` characters returns ``None`` (too broad to be useful).

    Examples::

        keyword_to_regex("merge conflict")  # → re.compile(r"merge\\ conflict", ...)
        keyword_to_regex("git*")            # → re.compile(r"git\\w*", ...)
        keyword_to_regex("*")               # → None
    """
    keyword = keyword.strip()
    if not keyword or not keyword.strip("*"):
        return None
    parts = keyword.split("*")
    escaped = r"\w*".join(re.escape(p) for p in parts)
    try:
        return re.compile(escaped, re.IGNORECASE)
    except re.error:
        return None


def match_keyword(keyword: str, text_lower: str) -> bool:
    """Return ``True`` if *keyword* matches anywhere in *text_lower*.

    Keyword matching is case-insensitive (caller should pass ``text.lower()``).
    Wildcards (``*``) expand to ``\\w*`` as per :func:`keyword_to_regex`.
    """
    pattern = keyword_to_regex(keyword)
    if pattern is None:
        return False
    return bool(pattern.search(text_lower))


# ---------------------------------------------------------------------------
# Skill-descriptor scoring (soft routing for skill SKILL.md files)
# ---------------------------------------------------------------------------

_DESCRIPTOR_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SHORT_DESCRIPTOR_TOKENS: frozenset[str] = frozenset(
    {"ai", "ci", "cd", "pr", "vm", "ui", "ux", "db", "id", "io", "os"}
)
# Tokens excluded from skill-descriptor scoring.  Tuned for an *agent* corpus:
# nearly every skill is "about" agents, tasks, tools, code and projects, so
# those tokens carry no routing signal and would make the descriptor score
# degrade toward a constant.  Shrinking this set measurably regresses
# routing (brain parity run, 2026-08-20: +2.8 on one skill from generic-token
# overlap alone).  Do not shrink it to a generic-English list.
_DESCRIPTOR_STOPWORDS: frozenset[str] = frozenset(
    {
        "about",
        "after",
        "agent",
        "agents",
        "and",
        "any",
        "are",
        "before",
        "build",
        "can",
        "code",
        "debug",
        "does",
        "for",
        "from",
        "get",
        "how",
        "into",
        "just",
        "make",
        "need",
        "only",
        "other",
        "our",
        "out",
        "over",
        "process",
        "project",
        "run",
        "set",
        "should",
        "task",
        "than",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "this",
        "those",
        "through",
        "tool",
        "tools",
        "use",
        "used",
        "using",
        "when",
        "where",
        "which",
        "with",
        "work",
        "your",
    }
)


def _descriptor_tokens(text: str) -> set[str]:
    """Tokenise routing-descriptor text into a set of normalised word tokens."""
    tokens: set[str] = set()
    for raw in _DESCRIPTOR_TOKEN_RE.findall(text.lower()):
        for part in re.split(r"[-_/]", raw):
            token = part.strip()
            if not token:
                continue
            if len(token) < 3 and token not in _SHORT_DESCRIPTOR_TOKENS:
                continue
            if token not in _DESCRIPTOR_STOPWORDS:
                tokens.add(token)
            # Strip trailing "s" for simple plurals (not "ss", "us", "is")
            if (
                token.endswith("s")
                and len(token) > 4
                and not token.endswith("ss")
                and not token.endswith("us")
                and not token.endswith("is")
            ):
                singular = token[:-1]
                if len(singular) >= 3 and singular not in _DESCRIPTOR_STOPWORDS:
                    tokens.add(singular)
    return tokens


def _score_skill_descriptor(lesson: dict[str, Any], prompt_lower: str) -> tuple[float, list[str]]:
    """Score a skill lesson's name/description/tags against the prompt.

    Returns ``(score, matched_by)`` where ``score > 0`` only for skill files
    (``is_skill=True``) with at least two matching descriptor tokens.
    """
    if not lesson.get("is_skill"):
        return 0.0, []

    prompt_tokens = _descriptor_tokens(prompt_lower)
    if not prompt_tokens:
        return 0.0, []

    skill_name = str(lesson.get("skill_name") or "")
    routing_text = str(lesson.get("when_to_use") or lesson.get("description") or "")
    tags = [str(t).strip() for t in (lesson.get("tags") or []) if str(t).strip()]

    name_overlap = prompt_tokens & _descriptor_tokens(skill_name)
    desc_overlap = prompt_tokens & _descriptor_tokens(routing_text)
    tag_overlap = prompt_tokens & _descriptor_tokens(" ".join(tags))
    total_overlap = name_overlap | desc_overlap | tag_overlap

    if len(total_overlap) < 2:
        return 0.0, []

    score = 0.0
    matched_by: list[str] = []
    if name_overlap:
        score += min(1.2, 0.6 * len(name_overlap))
        matched_by.append(f"name:{','.join(sorted(name_overlap)[:3])}")
    if desc_overlap:
        score += min(1.2, 0.35 * len(desc_overlap))
        matched_by.append(f"description:{','.join(sorted(desc_overlap)[:4])}")
    if tag_overlap:
        score += min(0.9, 0.45 * len(tag_overlap))
        matched_by.append(f"tags:{','.join(sorted(tag_overlap)[:3])}")
    return score, matched_by


# ---------------------------------------------------------------------------
# Lesson scanning
# ---------------------------------------------------------------------------


def scan_lessons(lesson_dirs: list[Path]) -> list[dict[str, Any]]:
    """Scan *lesson_dirs* and return a list of parsed lesson dicts.

    Each dict contains::

        {
            "path": str,               # absolute file path
            "title": str,              # H1 title or file stem
            "id": str | None,          # frontmatter ``id`` field
            "keywords": list[str],
            "patterns": list[str],     # full-regex patterns
            "skill_name": str | None,
            "description": str,
            "when_to_use": str,
            "tags": list[str],
            "harness_restrict": list[str],
            "session_categories": list[str],
            "is_skill": bool,
            "body": str,
            "n_keywords": int,
        }

    **Deduplication** (first-dir-wins, matching ``gptme#1594`` behaviour):

    1. By resolved path — handles symlinks to the same file.
    2. By filename — if ``lessons/X/foo.md`` exists, a later
       ``contrib/lessons/Y/foo.md`` is skipped, *whatever* its category dir.
       The name is registered when the file is first encountered, **before**
       the status/match-data filters, so a local lesson that has been retired
       (``status: archived|deprecated|automated``), moved under ``archived/``,
       or stripped of its keywords still silences the shared contrib copy.
       Otherwise archiving a lesson locally is undone by the next layer.
       An ``archive/`` copy registers its name only when the same dir has no
       active sibling of that name.

    ``SKILL.md`` files are never deduplicated by name.

    Lessons with ``status`` other than ``"active"`` are skipped.
    Lessons with no keywords, patterns, or skill name are skipped.
    """
    lessons: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_names: set[str] = set()  # filename-based dedup: first dir wins

    for lesson_dir in lesson_dirs:
        if not lesson_dir.exists():
            continue
        for f in sorted(lesson_dir.rglob("*.md")):
            if f.name == "README.md":
                continue
            # Skip tool/cache directories
            if _SKIP_DIR_PARTS & set(f.relative_to(lesson_dir).parts):
                continue

            resolved = str(f.resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            relative_path = f.relative_to(lesson_dir)
            is_skill_file = f.name == "SKILL.md"
            if "archive" in relative_path.parts:
                # An archived copy shadows later dirs unless this dir also
                # carries an active (non-archived) lesson with the same name.
                # rglob sorts ``archive/foo.md`` before ``foo.md`` so the
                # active sibling has to be looked up explicitly.
                if not is_skill_file and not any(
                    "archive" not in p.relative_to(lesson_dir).parts
                    for p in lesson_dir.rglob(f.name)
                ):
                    seen_names.add(f.name)
                continue

            if not is_skill_file:
                if f.name in seen_names:
                    continue
                seen_names.add(f.name)

            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue

            fm, body = extract_frontmatter(content)

            status = fm.get("status", "active")
            if status != "active":
                continue

            match_data = fm.get("match", {})
            if isinstance(match_data, dict):
                raw_keywords = match_data.get("keywords", [])
                raw_patterns = match_data.get("patterns", [])
            else:
                raw_keywords = []
                raw_patterns = []

            if isinstance(raw_keywords, str):
                raw_keywords = [raw_keywords]
            keywords = _dedupe_strings([*raw_keywords, *_string_list(fm.get("keywords"))])

            if isinstance(raw_patterns, str):
                raw_patterns = [raw_patterns]
            patterns = _dedupe_strings([*raw_patterns, *_string_list(fm.get("patterns"))])

            skill_name = fm.get("name") if isinstance(fm.get("name"), str) else None
            lesson_id = fm.get("id") if isinstance(fm.get("id"), str) else None
            description = fm.get("description") if isinstance(fm.get("description"), str) else ""
            when_to_use = fm.get("when_to_use") if isinstance(fm.get("when_to_use"), str) else ""
            metadata_value = fm.get("metadata")
            metadata = metadata_value if isinstance(metadata_value, dict) else {}
            tags = _string_list(metadata.get("tags"))
            harness_restrict = _string_list(metadata.get("harness"))

            _raw_sc = (
                match_data.get("session_categories") or [] if isinstance(match_data, dict) else []
            )
            session_categories = _dedupe_strings(_string_list(_raw_sc))

            if not keywords and not patterns and not skill_name:
                continue

            title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else f.stem

            lessons.append(
                {
                    "path": str(f),
                    "title": title,
                    "id": lesson_id,
                    "keywords": keywords,
                    "patterns": patterns,
                    "skill_name": skill_name,
                    "description": description,
                    "when_to_use": when_to_use,
                    "tags": tags,
                    "harness_restrict": harness_restrict,
                    "session_categories": session_categories,
                    "is_skill": f.name == "SKILL.md" or skill_name is not None,
                    "body": body,
                    "n_keywords": len(keywords),
                }
            )
    return lessons


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filter_by_harness(lessons: list[dict[str, Any]], harness: str) -> list[dict[str, Any]]:
    """Return lessons that are unrestricted or explicitly allowed for *harness*.

    Recognised harness strings: ``"claude-code"``, ``"gptme"``, ``"codex"``.
    Lessons without a ``harness_restrict`` list pass unconditionally.
    """
    filtered = []
    for lesson in lessons:
        restrict = lesson.get("harness_restrict") or []
        if not restrict:
            filtered.append(lesson)
            continue
        allowed = {str(v).strip().lower() for v in restrict if str(v).strip()}
        if harness.lower() in allowed:
            filtered.append(lesson)
    return filtered


def filter_by_session_category(
    lessons: list[dict[str, Any]], category: str | None
) -> list[dict[str, Any]]:
    """Return lessons that are unrestricted or match *category*.

    Lessons with a non-empty ``match.session_categories`` list are excluded
    unless *category* appears in that list (case-insensitive). Only the nested
    ``match:`` form is read — a top-level ``session_categories`` key is not the
    documented schema and is ignored, matching the Claude Code hook this ports.
    When *category* is ``None`` only unrestricted lessons (no list) are kept —
    this prevents social/triage/research lessons from firing when the session
    category is unknown. An empty string is compared as a known category rather
    than treated as ``None``.

    Examples::

        # "code" category: lessons restricted to ["code", "infrastructure"] pass
        # Lessons with no session_categories always pass
        filter_by_session_category(lessons, "code")

        # Unknown category: only unrestricted lessons pass
        filter_by_session_category(lessons, None)
    """
    filtered = []
    cat_lower = category.lower() if category is not None else None
    for lesson in lessons:
        cats = lesson.get("session_categories") or []
        if not cats or (cat_lower is not None and cat_lower in {c.lower() for c in cats}):
            filtered.append(lesson)
    return filtered


# ---------------------------------------------------------------------------
# Holdout filtering (A/B testing / LOO analysis)
# ---------------------------------------------------------------------------


def parse_holdout_set(value: str) -> set[str]:
    """Parse a comma-separated lesson-identifier string into a holdout set.

    Accepts file stem, filename, full or partial path, or frontmatter ``id``::

        parse_holdout_set("browser-verification,strategic/scope-discipline.md")
    """
    return {token.strip().lower().replace("\\", "/") for token in value.split(",") if token.strip()}


def is_held_out(lesson: dict[str, Any], holdout: set[str]) -> bool:
    """Return ``True`` if *lesson* matches any identifier in *holdout*."""
    if not holdout:
        return False
    path = Path(str(lesson["path"]))
    path_str = str(path).lower().replace("\\", "/")
    identifiers = {
        path_str,
        path.name.lower(),
        (path.parent.name if path.name.lower() == "skill.md" else path.stem).lower(),
    }
    lesson_id = lesson.get("id")
    if isinstance(lesson_id, str) and lesson_id.strip():
        identifiers.add(lesson_id.strip().lower())

    for token in holdout:
        if token in identifiers:
            return True
        if "/" in token or token.endswith(".md"):
            normalized = token.lstrip("./")
            # The docstring advertises "full or partial path", so a path-shaped
            # token may omit the ``.md`` suffix ("workflow/foo").  Try both.
            candidates = {normalized}
            if not normalized.endswith(".md"):
                candidates.add(f"{normalized}.md")
            for candidate in candidates:
                if path_str == candidate or path_str.endswith(f"/{candidate}"):
                    return True
    return False


def filter_held_out(lessons: list[dict[str, Any]], holdout: set[str]) -> list[dict[str, Any]]:
    """Remove lessons in *holdout* from the list."""
    if not holdout:
        return lessons
    return [lesson for lesson in lessons if not is_held_out(lesson, holdout)]


# ---------------------------------------------------------------------------
# BM25 index
# ---------------------------------------------------------------------------


def _build_bm25_index(lessons: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an in-memory BM25 index over lesson description/title/keywords.

    Returns a dict with ``corpus``, ``df``, ``N``, and ``avg_dl`` keys.
    """
    corpus: list[list[str]] = []
    for lesson in lessons:
        doc = " ".join(
            [
                lesson.get("description") or "",
                lesson.get("title") or "",
                " ".join(lesson.get("keywords") or []),
                lesson.get("when_to_use") or "",
            ]
        )
        corpus.append(re.findall(r"[a-z0-9]+", doc.lower()))

    N = len(corpus)
    avg_dl = sum(len(d) for d in corpus) / max(N, 1)
    df: dict[str, int] = {}
    for doc_tokens in corpus:
        for term in set(doc_tokens):
            df[term] = df.get(term, 0) + 1

    return {"corpus": corpus, "df": df, "N": N, "avg_dl": avg_dl}


def _bm25_score(query_terms: list[str], doc_terms: list[str], index: dict[str, Any]) -> float:
    """Compute BM25 score for *query_terms* against a document."""
    k1, b = BM25_K1, BM25_B
    N, avg_dl = index["N"], index["avg_dl"]
    dl = len(doc_terms)
    if not dl or not query_terms:
        return 0.0

    tf: dict[str, int] = {}
    for term in doc_terms:
        tf[term] = tf.get(term, 0) + 1

    df = index["df"]
    score = 0.0
    for term in query_terms:
        if term not in tf:
            continue
        tf_td = tf[term]
        df_t = df.get(term, 0)
        idf = math.log((N - df_t + 0.5) / (df_t + 0.5) + 1)
        tf_norm = (tf_td * (k1 + 1)) / (tf_td + k1 * (1 - b + b * dl / max(avg_dl, 1)))
        score += idf * tf_norm
    return score


def _bm25_min_z(n_nonzero: int) -> float:
    """Adaptive minimum z-score gate.

    When very few lessons overlap the query (``n_nonzero < 3``) the query is
    already highly discriminative and we admit matches on the raw floor alone.
    For larger corpora the z-score gate is the primary filter; this function
    caps it at what is actually attainable given the corpus size.
    """
    if n_nonzero < 3:
        return -math.inf
    max_attainable = (n_nonzero - 1) / math.sqrt(n_nonzero)
    return min(BM25_MIN_Z, BM25_STANDOUT_FRACTION * max_attainable)


def _bm25_zscores(scores: list[float]) -> list[float]:
    """Standardise raw BM25 scores against the nonzero score distribution.

    Lessons scoring 0 (no term overlap) receive ``0.0``.  If fewer than two
    lessons score nonzero the spread is degenerate and every z is ``0.0`` —
    nothing can "stand out" from a background of one.

    When all nonzero scores are identical (zero variance, e.g. two lessons with
    equal BM25 overlap) returning ``0.0`` would cause the z-gate to reject every
    lesson (since 0 < ``_bm25_min_z(n)`` for n≥2).  Instead, tied lessons receive
    a nominal z of ``1.0`` so the gate can still admit them.
    """
    nonzero = [s for s in scores if s > 0]
    if len(nonzero) < 2:
        return [0.0] * len(scores)
    mean = sum(nonzero) / len(nonzero)
    var = sum((s - mean) ** 2 for s in nonzero) / len(nonzero)
    sd = math.sqrt(var)
    if sd <= 0:
        # All nonzero scores are identical — give each a nominal z of 1.0 so
        # the z-gate can admit them rather than silently dropping all of them.
        return [1.0 if s > 0 else 0.0 for s in scores]
    return [(s - mean) / sd if s > 0 else 0.0 for s in scores]


# ---------------------------------------------------------------------------
# Main scoring entry point
# ---------------------------------------------------------------------------


def score_lessons(
    lessons: list[dict[str, Any]],
    prompt: str,
    max_results: int = 5,
    *,
    use_bm25: bool = True,
) -> list[dict[str, Any]]:
    """Match *lessons* against *prompt* and return the top ``max_results`` hits.

    Each returned dict is a copy of the input lesson dict augmented with:

    * ``"score"``: float — combined score (keyword + BM25 contributions).
    * ``"matched_by"``: list[str] — human-readable match reasons.

    Scoring model (additive):

    1. +1.0 per matched keyword (wildcard-aware).
    2. +1.0 per matched pattern (full regex).
    3. +1.5 for skill-name exact/near match.
    4. +0–3.3 for skill-descriptor token overlap.
    5. ``BM25_WEIGHT × z-score`` for semantic BM25 overlap (when ``use_bm25``
       is True and the lesson passes the z-score + raw-floor gate).

    Thompson-sampling re-ranking is deliberately excluded here — callers with
    TS state should add ``TS_WEIGHT × posterior_mean`` to each returned score
    before final sorting.

    Args:
        lessons: Parsed lessons as returned by :func:`scan_lessons`.
        prompt: The query text (not pre-lowercased; handled internally).
        max_results: Maximum number of results to return.
        use_bm25: Enable BM25 semantic scoring (True by default).

    Returns:
        List of matched lesson dicts, sorted descending by score, capped at
        ``max_results``.
    """
    prompt_lower = prompt.lower()
    query_terms = re.findall(r"[a-z0-9]+", prompt_lower) if use_bm25 else []

    # Compute BM25 scores up front (gated relative to THIS query's distribution)
    bm_scores: list[float] = []
    bm_zs: list[float] = []
    bm_min_z = math.inf
    bm_n_nonzero = 0
    if use_bm25 and query_terms:
        bm_index = _build_bm25_index(lessons)
        bm_scores = [
            _bm25_score(query_terms, doc_terms, bm_index) for doc_terms in bm_index["corpus"]
        ]
        bm_zs = _bm25_zscores(bm_scores)
        bm_n_nonzero = sum(1 for s in bm_scores if s > 0)
        bm_min_z = _bm25_min_z(bm_n_nonzero)

    results: list[dict[str, Any]] = []

    for i, lesson in enumerate(lessons):
        score = 0.0
        matched_by: list[str] = []

        # 1. Keyword matching (wildcard-aware)
        for kw in lesson["keywords"]:
            if match_keyword(kw, prompt_lower):
                score += 1.0
                matched_by.append(kw)

        # 2. Pattern matching (full regex, case-insensitive so patterns with
        #    uppercase chars like "GitHub" or "[A-Z].*" match prompt_lower).
        #    Lessons can come from shared repositories, so bound each search.
        for pat in lesson["patterns"]:
            try:
                if regex.search(
                    pat,
                    prompt_lower,
                    regex.IGNORECASE,
                    timeout=PATTERN_TIMEOUT_SECONDS,
                ):
                    score += 1.0
                    matched_by.append(f"pattern:{pat[:30]}")
            except (regex.error, TimeoutError):
                pass

        # 3. Skill name exact / near-match
        if lesson.get("skill_name"):
            name_lower = lesson["skill_name"].lower()
            for variant in [name_lower, name_lower.replace("-", " ")]:
                if variant in prompt_lower:
                    score += 1.5
                    matched_by.append(f"skill:{lesson['skill_name']}")
                    break

        # 4. Skill descriptor token overlap
        desc_score, desc_matches = _score_skill_descriptor(lesson, prompt_lower)
        if desc_score > 0:
            score += desc_score
            matched_by.extend(desc_matches)

        # 5. BM25 semantic scoring
        if bm_scores:
            bm_raw = bm_scores[i]
            bm_z = bm_zs[i]
            corpus_below_floor = max(bm_scores) < BM25_MIN_RAW
            passes_raw_gate = bm_raw >= BM25_MIN_RAW or (corpus_below_floor and bm_raw > 0)
            # With two overlaps, only the positive-z standout should contribute;
            # flooring the weaker negative-z hit would over-credit noise.
            passes_z_gate = bm_z >= bm_min_z and not (bm_n_nonzero == 2 and bm_z <= 0)
            if passes_z_gate and passes_raw_gate:
                # Use z-score as the contribution so ranking is preserved.
                # Edge case: one nonzero score has a degenerate z-score of 0,
                # so give that sole overlap a neutral contribution of 1.0.
                bm_contribution = bm_z if bm_n_nonzero >= 2 else 1.0
                score += BM25_WEIGHT * bm_contribution
                matched_by.append(f"bm25:{bm_raw:.2f}")

        if score > 0:
            results.append({**lesson, "score": score, "matched_by": matched_by})

    results.sort(key=lambda x: -x["score"])
    return results[:max_results]
