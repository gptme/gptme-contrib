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
import importlib.util
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, cast

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
    # Try "category/stem" first (avoids collisions), fall back to bare stem
    if effectiveness:
        lesson_key = f"{path.parent.name}/{path.stem}"
        score = effectiveness.get(lesson_key)
        if score is None:
            score = effectiveness.get(path.stem)
        lesson.effectiveness_score = score

    return lesson


def load_lessons(lessons_dir: Path, state_file: Path | None = None) -> list[Lesson]:
    """Load all active lessons from a directory tree."""
    effectiveness: dict[str, float] = {}

    # Load LOO effectiveness scores if available
    if state_file and state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            if "results" in data and isinstance(data["results"], list):
                # LOO state format: {"meta": ..., "results": [{"path": stem, "delta": score}]}
                effectiveness = {
                    r["path"]: r["delta"]
                    for r in data["results"]
                    if isinstance(r.get("path"), str)
                    and isinstance(r.get("delta"), int | float)
                }
            else:
                # Simple dict format: {"category/stem": score}
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


@dataclass
class MemoryBackend:
    memory_dir: Path
    state_file: Path
    module: ModuleType

    def discover_entries(self) -> list[Any]:
        return cast(list[Any], self.module.discover_memory_entries(self.memory_dir))

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.module.select_relevant_memories(
                query,
                memory_dir=self.memory_dir,
                state_file=self.state_file,
                limit=limit,
            ),
        )


def _discover_agent_root(search_from: list[Path]) -> Path | None:
    seen: set[Path] = set()
    for base in search_from:
        for candidate in [base, *base.parents]:
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "memory").is_dir() and (
                candidate / "scripts" / "memory" / "memory_retrieval.py"
            ).exists():
                return candidate
    return None


def _load_memory_backend(
    memory_dir: Path | None,
    memory_state_file: Path | None,
) -> MemoryBackend | None:
    if memory_dir is None:
        agent_root = _discover_agent_root([Path.cwd(), Path(__file__).resolve()])
        if agent_root is None:
            return None
        memory_dir = agent_root / "memory"
        memory_state_file = agent_root / "state" / "cc-memory" / "metadata.json"

    memory_dir = memory_dir.expanduser().resolve()
    if memory_state_file is None:
        memory_state_file = memory_dir.parent / "state" / "cc-memory" / "metadata.json"
    else:
        memory_state_file = memory_state_file.expanduser().resolve()

    retrieval_script = memory_dir.parent / "scripts" / "memory" / "memory_retrieval.py"
    if not memory_dir.exists() or not retrieval_script.exists():
        return None

    module_name = f"agent_memory_retrieval_{retrieval_script.resolve()}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        retrieval_script,
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[spec.name]
        print(f"Warning: failed to load memory module: {exc}", file=sys.stderr)
        return None
    return MemoryBackend(
        memory_dir=memory_dir,
        state_file=memory_state_file,
        module=module,
    )


def _normalize_memory_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _find_memory_entry(
    backend: MemoryBackend, name: str
) -> tuple[Any | None, list[str]]:
    query = _normalize_memory_name(name)
    if not query:
        return None, []

    exact: Any | None = None
    partials: list[Any] = []

    for entry in backend.discover_entries():
        candidates = {
            _normalize_memory_name(entry.key),
            _normalize_memory_name(entry.path.stem),
            _normalize_memory_name(entry.name),
            *[_normalize_memory_name(alias) for alias in entry.aliases],
        }
        if query in candidates:
            exact = entry
            break
        if any(query in candidate for candidate in candidates if candidate):
            partials.append(entry)

    if exact is not None:
        return exact, []
    if len(partials) == 1:
        return partials[0], []
    suggestions = [entry.path.name for entry in partials[:5]]
    return None, suggestions


# ── MCP server ────────────────────────────────────────────────────────────────


def build_server(
    lessons: list[Lesson],
    *,
    memory_dir: Path | None = None,
    memory_state_file: Path | None = None,
):
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
    memory_backend = _load_memory_backend(memory_dir, memory_state_file)

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

    @mcp.resource("lessons://lesson/{category}/{name}")
    def get_lesson(category: str, name: str) -> str:
        """Full content of a specific lesson."""
        lesson_id = f"{category}/{name}"
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
            lines.append(f"Resource: `lessons://lesson/{ls.category}/{ls.path.stem}`\n")
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

    @mcp.tool()
    def memory_search(query: str, limit: int = 5) -> str:
        """Search durable memory entries using the agent's memory retrieval scorer.

        Args:
            query: Natural-language or alias query for memory/*.md entries
            limit: Maximum number of matches to return (default: 5)
        """
        if memory_backend is None:
            return (
                "Memory support unavailable. Configure --memory-dir or run the server "
                "inside an agent workspace with memory/*.md and scripts/memory/"
                "memory_retrieval.py."
            )

        results = memory_backend.search(query, limit=max(1, min(limit, 10)))
        if not results:
            return f"No memory entries found matching: {query!r}"

        lines = [
            f"Found {len(results)} memory entr{'y' if len(results) == 1 else 'ies'} for {query!r}:\n"
        ]
        for entry in results:
            lines.append(
                f"**{entry['name']}** (`{entry['key']}`) "
                f"[type={entry['type']}, score={entry['score']:.3f}]"
            )
            if entry["description"]:
                lines.append(f"Description: {entry['description']}")
            if entry["matched_terms"]:
                lines.append(f"Match: {', '.join(entry['matched_terms'])}")
            lines.append(
                f"Confidence: {entry['confidence']:.2f} | Recency: {entry['recency']:.3f}"
            )
            if entry["excerpt"]:
                lines.append(f"Excerpt: {entry['excerpt']}")
            lines.append("")
        return "\n".join(lines).rstrip()

    @mcp.tool()
    def memory_get(name: str) -> str:
        """Return a specific durable memory entry by filename, stem, or alias.

        Args:
            name: Memory filename, stem, or alias (for example
                'feedback_greptile_review_loop' or 'greptile review loop')
        """
        if memory_backend is None:
            return (
                "Memory support unavailable. Configure --memory-dir or run the server "
                "inside an agent workspace with memory/*.md and scripts/memory/"
                "memory_retrieval.py."
            )

        entry, suggestions = _find_memory_entry(memory_backend, name)
        if entry is None:
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            return f"Memory entry not found: {name!r}.{hint}"

        aliases = ", ".join(entry.aliases[:5]) if entry.aliases else "none"
        lines = [
            f"# {entry.name}",
            f"File: {entry.path.name}",
            f"Type: {entry.type}",
            f"Description: {entry.description or 'none'}",
            f"Aliases: {aliases}",
            "",
            entry.body,
        ]
        return "\n".join(lines).rstrip()

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
    # Auto-discover LOO state file relative to lessons-dir (agent workspace convention)
    _default_loo = (
        Path(__file__).parent.parent / "state" / "lesson-thompson" / "loo-results.json"
    )
    parser.add_argument(
        "--effectiveness-file",
        type=Path,
        default=_default_loo if _default_loo.exists() else None,
        help=(
            "Path to JSON file with lesson effectiveness scores. "
            "Supports both {'category/stem': score} and the LOO state format "
            "({'results': [{'path': stem, 'delta': score}]}). "
            f"Auto-detected from: {_default_loo}"
        ),
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help=(
            "Path to memory directory. Defaults to auto-detecting an enclosing "
            "agent workspace with memory/*.md entries."
        ),
    )
    parser.add_argument(
        "--memory-state-file",
        type=Path,
        default=None,
        help=(
            "Path to memory metadata JSON. Defaults to "
            "<memory-dir>/../state/cc-memory/metadata.json."
        ),
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
    memory_dir = args.memory_dir.expanduser() if args.memory_dir else None
    memory_state_file = (
        args.memory_state_file.expanduser() if args.memory_state_file else None
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
    mcp = build_server(
        lessons,
        memory_dir=memory_dir,
        memory_state_file=memory_state_file,
    )
    mcp.run()


if __name__ == "__main__":
    main()
