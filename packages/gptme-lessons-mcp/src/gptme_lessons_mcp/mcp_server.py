"""MCP server that exposes gptme's lesson matching system as MCP tools.

Allows any MCP client (Claude Code, Cursor, Continue.dev, etc.) to query
"what lessons match this context?" so agents can access durable behavioral
knowledge across runtimes and sessions.

Usage:
    uv run gptme-lessons-mcp                            # stdio, auto-discover dirs
    uv run gptme-lessons-mcp --lessons-dir ~/my/lessons # explicit dir (repeatable)
    uv run gptme-lessons-mcp --lessons-dir dir1 --lessons-dir dir2

Tools exposed:
    match_lessons(context, top_k?)    → list of matched lessons with content
    list_lessons(category?, search?)  → list all active lessons
    get_lesson(path)                  → full body of one lesson by relative path
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Module-level lesson dirs override — set by main() before mcp.run()
_extra_lesson_dirs: list[Path] = []

mcp = FastMCP(
    "gptme-lessons",
    instructions=(
        "Access gptme's lesson library — durable behavioral rules for agents.\n"
        "`match_lessons` finds lessons relevant to a given context string (e.g. the "
        "current user message or task description).\n"
        "`list_lessons` enumerates available lessons, optionally by category.\n"
        "`get_lesson` retrieves the full text of one lesson by its relative path.\n\n"
        "Lessons are structured guidance files with YAML frontmatter and keyword-based "
        "triggering. They encode failure modes, anti-patterns, and proven workflows "
        "that agents should follow."
    ),
)


def _load_index():
    """Load lesson index, optionally with extra dirs prepended."""
    from gptme.lessons.index import LessonIndex

    if _extra_lesson_dirs:
        return LessonIndex(lesson_dirs=_extra_lesson_dirs)
    return LessonIndex()


def _lesson_to_dict(lesson, include_body: bool = True, max_body: int = 2000) -> dict:
    """Serialize a Lesson to a plain dict for MCP return."""
    result = {
        "path": str(lesson.path),
        "title": lesson.title,
        "category": lesson.category,
        "description": lesson.description,
        "keywords": lesson.metadata.keywords,
        "status": lesson.metadata.status,
    }
    if include_body:
        body = lesson.body
        if len(body) > max_body:
            body = body[:max_body] + "\n…(truncated)"
        result["body"] = body
    return result


@mcp.tool()
def match_lessons(
    context: str,
    top_k: int = 5,
) -> list[dict]:
    """Find lessons relevant to the given context string.

    Uses gptme's keyword-based lesson matcher to surface behavioral rules,
    anti-patterns, and proven workflows that apply to the current situation.

    Args:
        context: The text to match against (e.g. user message, task description,
                 error output, or a description of what you're about to do).
        top_k: Maximum number of lessons to return (1–20). Default 5.

    Returns:
        List of matching lessons, sorted by relevance score (descending). Each entry has:
            title, category, description, keywords, body (truncated at 2000 chars),
            score, matched_by (which keywords triggered the match)
    """
    top_k = max(1, min(20, top_k))

    from gptme.lessons.matcher import LessonMatcher, MatchContext

    index = _load_index()
    if not index.lessons:
        return []

    matcher = LessonMatcher()
    ctx = MatchContext(message=context)
    matches = matcher.match(index.lessons, ctx)[:top_k]

    results = []
    for m in matches:
        d = _lesson_to_dict(m.lesson)
        d["score"] = round(m.score, 3)
        d["matched_by"] = m.matched_by
        results.append(d)
    return results


@mcp.tool()
def list_lessons(
    category: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    """List active lessons, optionally filtered by category or text search.

    Args:
        category: Filter by category name (e.g. "tools", "patterns", "workflow",
                  "infrastructure"). Pass None to list all categories.
        search: Case-insensitive text filter applied to title, description, and
                keywords. Pass None to skip filtering.

    Returns:
        List of lessons (without body). Each entry has:
            path, title, category, description, keywords, status
    """
    index = _load_index()

    lessons = index.lessons
    if category:
        lessons = [lesson for lesson in lessons if lesson.category == category]
    if search:
        q = search.lower()
        lessons = [
            lesson
            for lesson in lessons
            if q in lesson.title.lower()
            or q in lesson.description.lower()
            or any(q in kw.lower() for kw in lesson.metadata.keywords)
        ]

    return [_lesson_to_dict(lesson, include_body=False) for lesson in lessons]


@mcp.tool()
def get_lesson(path: str) -> dict:
    """Get the full content of a lesson by its relative path or title substring.

    Prefer using the `path` value returned by `match_lessons` or `list_lessons`.
    If an exact path is not found, falls back to substring match on lesson title.

    Args:
        path: Relative file path (e.g. "tools/git-safe-commit.md") or title
              substring to search for.

    Returns:
        Full lesson dict including the complete body text, or an error key if
        no lesson matched.
    """
    index = _load_index()

    # Try exact path match (substring of full path)
    for lesson in index.lessons:
        if path in str(lesson.path):
            return _lesson_to_dict(lesson, max_body=10000)

    # Fallback: title substring
    path_lower = path.lower()
    for lesson in index.lessons:
        if path_lower in lesson.title.lower():
            return _lesson_to_dict(lesson, max_body=10000)

    return {"error": f"No lesson found for: {path!r}"}


@mcp.tool()
def list_categories() -> list[str]:
    """List all categories present in the lesson library.

    Returns:
        Sorted list of unique category names (e.g. ["infrastructure", "patterns",
        "tools", "workflow", ...]).
    """
    index = _load_index()
    return sorted({lesson.category for lesson in index.lessons})


def main() -> None:
    """Entry point for `gptme-lessons-mcp` script."""
    global _extra_lesson_dirs

    parser = argparse.ArgumentParser(
        description=(
            "gptme Lessons MCP Server — expose gptme's lesson library as MCP tools "
            "so any agent runtime can query behavioral knowledge."
        )
    )
    parser.add_argument(
        "--lessons-dir",
        type=Path,
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "Path to a lessons directory (repeatable). When provided, ONLY these "
            "directories are searched (gptme config defaults are ignored). "
            "Example: --lessons-dir ~/my-agent/lessons --lessons-dir ~/shared/lessons"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    if args.lessons_dir:
        _extra_lesson_dirs = [d.expanduser().resolve() for d in args.lessons_dir]
        missing = [d for d in _extra_lesson_dirs if not d.exists()]
        if missing:
            for d in missing:
                logger.warning("Lessons dir does not exist: %s", d)

    mcp.run()


if __name__ == "__main__":
    main()
