#!/usr/bin/env python3
"""gptme-lessons MCP Server — expose gptme lessons as queryable MCP resources.

Serves gptme lessons (Markdown + YAML frontmatter) as MCP resources and tools,
making them accessible to any MCP-capable agent regardless of framework.

Design doc: knowledge/technical-designs/gptme-lessons-mcp.md

Usage:
    # Standalone
    python3 scripts/gptme-lessons-mcp.py --lessons-dir /path/to/lessons

    # Configure in Claude Code settings.json or mcp config:
    {
      "gptme-lessons": {
        "command": "python3",
        "args": ["path/to/gptme-lessons-mcp.py", "--lessons-dir", "~/bob/lessons"]
      }
    }

Dependencies:
    pip install mcp  # or: uv add mcp
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# ── Lesson loading ────────────────────────────────────────────────────────────


@dataclass
class Lesson:
    path: Path
    title: str
    category: str
    keywords: list[str] = field(default_factory=list)
    status: str = "active"
    content: str = ""
    effectiveness_score: float | None = None

    @property
    def id(self) -> str:
        """Stable ID: category/stem, e.g. 'workflow/autonomous-run'."""
        return f"{self.category}/{self.path.stem}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "keywords": self.keywords,
            "status": self.status,
            "effectiveness_score": self.effectiveness_score,
        }


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


_KEYWORDS_RE = re.compile(
    r"match:\s*\n\s+keywords:\s*\n((?:\s+-[^\n]+\n?)+)", re.MULTILINE
)
_KW_ITEM_RE = re.compile(r'^\s+-\s+"?([^"\n]+)"?\s*$', re.MULTILINE)


def _extract_keywords(frontmatter: str) -> list[str]:
    """Extract keywords from match.keywords block in frontmatter."""
    m = _KEYWORDS_RE.search(frontmatter)
    if not m:
        return []
    return [kw.strip().strip('"') for kw in _KW_ITEM_RE.findall(m.group(0))]


def _parse_yaml_simple(text: str) -> dict:
    """Minimal YAML parser for lesson frontmatter (avoids PyYAML dependency)."""
    result: dict = {}
    current_list: list | None = None

    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("  - "):
            # List item under current key
            if current_list is not None:
                current_list.append(line.strip()[2:].strip().strip('"'))
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"')
            if val == "":
                current_list = []
                result[key] = current_list
            else:
                result[key] = val
                current_list = None

    return result


def load_lesson(
    path: Path, effectiveness: dict[str, float] | None = None
) -> Lesson | None:
    """Parse a lesson Markdown file into a Lesson object."""
    try:
        text = path.read_text()
    except OSError:
        return None

    # Extract frontmatter + body
    m = _FRONTMATTER_RE.match(text)
    if not m:
        body = text
        meta: dict = {}
    else:
        meta = _parse_yaml_simple(m.group(1))
        body = m.group(2)

    # Status filter — skip non-active
    status = meta.get("status", "active")
    if status in ("deprecated", "archived"):
        return None

    # Extract title from first H1 in body
    tm = _TITLE_RE.search(body)
    title = tm.group(1) if tm else path.stem.replace("-", " ").title()

    # Keywords — use regex extractor to handle nested match.keywords block
    keywords = _extract_keywords(m.group(1)) if m else []

    # Category from directory name
    category = path.parent.name

    lesson = Lesson(
        path=path,
        title=title,
        category=category,
        keywords=keywords if isinstance(keywords, list) else [],
        status=status,
        content=text,
    )

    # Attach effectiveness score if available
    # Key by "category/stem" (matches Lesson.id) to avoid collisions across categories
    if effectiveness:
        lesson_key = f"{path.parent.name}/{path.stem}"
        lesson.effectiveness_score = effectiveness.get(lesson_key)

    return lesson


def load_lessons(lessons_dir: Path, state_file: Path | None = None) -> list[Lesson]:
    """Load all active lessons from a directory tree."""
    effectiveness: dict[str, float] = {}

    # Load LOO effectiveness scores if available
    if state_file and state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            # Expected format: {"category/stem": score, ...} (e.g. "workflow/autonomous-run")
            effectiveness = {
                k: v for k, v in data.items() if isinstance(v, int | float)
            }
        except (json.JSONDecodeError, OSError):
            pass

    lessons = []
    for md_file in sorted(lessons_dir.rglob("*.md")):
        if md_file.name == "README.md":
            continue
        lesson = load_lesson(md_file, effectiveness)
        if lesson:
            lessons.append(lesson)

    return lessons


# ── Search ────────────────────────────────────────────────────────────────────


def search_lessons_by_query(
    lessons: list[Lesson], query: str, category: str | None = None
) -> list[Lesson]:
    """Rank lessons by keyword/title match against query. Returns top 10."""
    q_lower = query.lower()
    scored = []

    for lesson in lessons:
        if category and lesson.category != category:
            continue
        score = 0.0
        # Title match
        if q_lower in lesson.title.lower():
            score += 3.0
        # Keyword match
        for kw in lesson.keywords:
            if q_lower in kw.lower() or kw.lower() in q_lower:
                score += 2.0
        # Content match (lower weight, broad signal)
        if q_lower in lesson.content.lower():
            score += 0.5
        if score > 0:
            scored.append((score, lesson))

    scored.sort(key=lambda x: -x[0])
    return [lesson for _, lesson in scored[:10]]


# ── MCP server ────────────────────────────────────────────────────────────────


def build_server(lessons: list[Lesson]):
    """Build the FastMCP server. Separated for testability."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "Error: 'mcp' package not installed. Run: pip install mcp", file=sys.stderr
        )
        sys.exit(1)

    mcp = FastMCP("gptme-lessons")
    active_lessons = [ls for ls in lessons if ls.status == "active"]
    lesson_by_id = {ls.id: ls for ls in active_lessons}

    # ── Resources ────────────────────────────────────────────────────────────

    @mcp.resource("lessons://index")
    def get_index() -> str:
        """Index of all available lessons with metadata."""
        lines = [f"# gptme Lessons Index ({len(active_lessons)} lessons)\n"]
        for cat in sorted({ls.category for ls in active_lessons}):
            cat_lessons = [ls for ls in active_lessons if ls.category == cat]
            lines.append(f"\n## {cat} ({len(cat_lessons)})")
            for ls in cat_lessons:
                eff = (
                    f" [eff={ls.effectiveness_score:.2f}]"
                    if ls.effectiveness_score is not None
                    else ""
                )
                lines.append(f"- `{ls.id}`: {ls.title}{eff}")
        return "\n".join(lines)

    @mcp.resource("lessons://category/{category}")
    def get_category(category: str) -> str:
        """All lessons in a category."""
        cat_lessons = [ls for ls in active_lessons if ls.category == category]
        if not cat_lessons:
            return f"No lessons found in category: {category}"
        lines = [f"# {category.title()} Lessons\n"]
        for ls in cat_lessons:
            lines.append(f"## {ls.title}\n")
            lines.append(ls.content)
            lines.append("\n---\n")
        return "\n".join(lines)

    @mcp.resource("lessons://lesson/{lesson_id:path}")
    def get_lesson(lesson_id: str) -> str:
        """Full content of a specific lesson."""
        lesson = lesson_by_id.get(lesson_id)
        if not lesson:
            return f"Lesson not found: {lesson_id}"
        return lesson.content

    # ── Tools ─────────────────────────────────────────────────────────────────

    @mcp.tool()
    def search_lessons(query: str, category: str | None = None) -> str:
        """Search lessons by keyword, title, or description.

        Args:
            query: Search query (natural language or keyword)
            category: Optional filter by category (e.g. 'workflow', 'tools', 'patterns')
        """
        results = search_lessons_by_query(active_lessons, query, category)
        if not results:
            return f"No lessons found matching: {query!r}"
        lines = [f"Found {len(results)} lesson(s) for {query!r}:\n"]
        for ls in results:
            eff = (
                f" (effectiveness: {ls.effectiveness_score:.2f})"
                if ls.effectiveness_score is not None
                else ""
            )
            kws = ", ".join(ls.keywords[:3]) if ls.keywords else "none"
            lines.append(f"**{ls.title}** (`{ls.id}`){eff}")
            lines.append(f"Keywords: {kws}")
            lines.append(f"Resource: `lessons://lesson/{ls.id}`\n")
        return "\n".join(lines)

    @mcp.tool()
    def get_effective_lessons(min_effectiveness: float = 0.10) -> str:
        """Get lessons with measured effectiveness above threshold.

        Args:
            min_effectiveness: Minimum LOO effectiveness score (default: 0.10)
        """
        effective = [
            ls
            for ls in active_lessons
            if ls.effectiveness_score is not None
            and ls.effectiveness_score >= min_effectiveness
        ]
        if not effective:
            return (
                f"No lessons with effectiveness >= {min_effectiveness}. "
                "Effectiveness data may not be loaded."
            )
        effective.sort(key=lambda ls: -(ls.effectiveness_score or 0))
        lines = [f"Top lessons by effectiveness (threshold: {min_effectiveness}):\n"]
        for ls in effective:
            lines.append(f"- **{ls.title}** (`{ls.id}`): {ls.effectiveness_score:.2f}")
        return "\n".join(lines)

    @mcp.tool()
    def get_lesson_context(situation: str) -> str:
        """Get relevant lessons for a described situation or problem.

        Args:
            situation: Description of what you're doing or what went wrong
        """
        results = search_lessons_by_query(active_lessons, situation)
        if not results[:3]:
            return f"No relevant lessons found for: {situation!r}"
        lines = [f"Relevant lessons for: {situation!r}\n"]
        for ls in results[:3]:
            lines.append(f"## {ls.title}\n")
            # Include first 40 lines of lesson content
            content_preview = "\n".join(ls.content.splitlines()[:40])
            lines.append(content_preview)
            lines.append("\n---\n")
        return "\n".join(lines)

    @mcp.tool()
    def list_categories() -> str:
        """List all lesson categories with lesson counts."""
        counts = Counter(ls.category for ls in active_lessons)
        lines = ["Available lesson categories:\n"]
        for cat, count in sorted(counts.items()):
            lines.append(f"- **{cat}** ({count} lessons)")
        return "\n".join(lines)

    return mcp


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="gptme lessons MCP server")
    parser.add_argument(
        "--lessons-dir",
        type=Path,
        default=Path(__file__).parent.parent / "lessons",
        help="Path to lessons directory (default: ../lessons)",
    )
    parser.add_argument(
        "--effectiveness-file",
        type=Path,
        default=None,
        help="Path to JSON file with lesson effectiveness scores",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List loaded lessons and exit (no server)",
    )
    args = parser.parse_args()

    lessons_dir = args.lessons_dir.expanduser()
    effectiveness_file = (
        args.effectiveness_file.expanduser() if args.effectiveness_file else None
    )

    if not lessons_dir.exists():
        print(f"Error: lessons dir not found: {lessons_dir}", file=sys.stderr)
        sys.exit(1)

    lessons = load_lessons(lessons_dir, effectiveness_file)

    if args.list:
        for ls in lessons:
            eff = (
                f" [{ls.effectiveness_score:.2f}]"
                if ls.effectiveness_score is not None
                else ""
            )
            print(f"{ls.id}{eff}")
        print(f"\nTotal: {len(lessons)} lessons", file=sys.stderr)
        return

    print(f"Loaded {len(lessons)} lessons from {lessons_dir}", file=sys.stderr)
    mcp = build_server(lessons)
    mcp.run()


if __name__ == "__main__":
    main()
