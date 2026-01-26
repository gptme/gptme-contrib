#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "rich>=13.0.0",
#     "python-frontmatter>=1.0.0",
# ]
# [tool.uv]
# exclude-newer = "2024-04-01T00:00:00Z"
# ///

"""Lesson usage analytics for gptme agents.

Analyzes conversation logs to track lesson usage, effectiveness,
and identify potential gaps in the lessons system. Provides insights into:
- Which lessons are most/least used
- Keyword effectiveness for lesson matching
- Category coverage and gaps
- Recent lesson activity patterns

The analytics scan conversation.jsonl files in gptme logs directory, searching
for lesson path references and title mentions. Results are presented in rich
tables and can be saved to markdown reports.

Example:
    Run analytics and generate report::

        $ python -m lessons.analytics

    Output includes:
    - Top 20 most-used lessons
    - Never-used lessons (potential cleanup candidates)
    - Category breakdown with average references
    - Keyword analysis showing trigger effectiveness
    - Recent lesson reference activity
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

from rich.console import Console
from rich.table import Table
import frontmatter


@dataclass
class LessonReference:
    """A reference to a lesson in a conversation.

    Captures a single instance of a lesson being mentioned or referenced
    during a conversation, including context for analysis.

    Attributes:
        lesson_path: Relative path to the lesson file from lessons directory.
        conversation_id: Unique identifier for the conversation.
        timestamp: When the reference occurred.
        context: Surrounding text (100 chars before/after) for context.
        role: Who referenced it - "user" or "assistant".
    """

    lesson_path: str
    conversation_id: str
    timestamp: datetime
    context: str
    role: str  # user or assistant


@dataclass
class LessonStats:
    """Statistics for a lesson.

    Aggregates all usage data for a single lesson across all conversations.

    Attributes:
        path: Relative path to the lesson file from lessons directory.
        title: Lesson title extracted from markdown heading.
        category: Lesson category (directory name).
        keywords: List of keywords from frontmatter for matching.
        reference_count: Total number of times lesson was referenced.
        conversations: Set of conversation IDs where lesson appeared.
        last_referenced: Timestamp of most recent reference, or None if never used.
        contexts: List of context snippets from all references.
    """

    path: str
    title: str
    category: str
    keywords: List[str]
    reference_count: int
    conversations: Set[str]
    last_referenced: datetime | None
    contexts: List[str]


def find_lesson_files(lessons_dir: Path) -> Dict[str, Tuple[str, str, List[str]]]:
    """Find all lesson files and extract their titles, categories, and keywords.

    Scans the lessons directory recursively for .md files (excluding README, TODO,
    and templates). Parses frontmatter for keywords and extracts title from first
    markdown heading.

    Args:
        lessons_dir: Path to the lessons directory to scan.

    Returns:
        Dict mapping lesson relative path to (title, category, keywords) tuple.
    """
    lessons = {}

    # Scan all lesson markdown files (excluding README, TODO, template)
    for lesson_file in lessons_dir.rglob("*.md"):
        if lesson_file.name in ("README.md", "TODO.md", "lesson-template.md"):
            continue

        # Get category from directory name (default)
        category = lesson_file.parent.name
        keywords = []

        # Parse file with frontmatter support
        try:
            with open(lesson_file) as f:
                post = frontmatter.load(f)

                # Extract keywords from frontmatter if present
                if "match" in post.metadata and "keywords" in post.metadata["match"]:
                    keywords = post.metadata["match"]["keywords"]

                # Extract title from first heading in content
                match = re.search(r"^# (.+)$", post.content, re.MULTILINE)
                title = match.group(1) if match else lesson_file.stem
        except Exception:
            # Fallback to old behavior if parsing fails
            try:
                with open(lesson_file) as f:
                    content = f.read()
                    match = re.search(r"^# (.+)$", content, re.MULTILINE)
                    title = match.group(1) if match else lesson_file.stem
            except Exception:
                title = lesson_file.stem

        # Store relative path from lessons directory
        rel_path = str(lesson_file.relative_to(lessons_dir))
        lessons[rel_path] = (title, category, keywords)

    return lessons


def extract_lesson_references(
    conversation_file: Path, lessons: Dict[str, Tuple[str, str, List[str]]]
) -> List[LessonReference]:
    """Extract all lesson references from a conversation log.

    Args:
        conversation_file: Path to conversation.jsonl
        lessons: Dict of lesson paths to (title, category)

    Returns:
        List of LessonReference objects
    """
    references = []
    conversation_id = conversation_file.parent.name

    try:
        with open(conversation_file) as f:
            for line in f:
                try:
                    msg = json.loads(line)

                    # Skip system messages
                    if msg.get("role") == "system":
                        continue

                    content = msg.get("content", "")
                    timestamp = datetime.fromisoformat(msg.get("timestamp"))
                    role = msg.get("role", "unknown")

                    # Look for direct path references
                    for lesson_path in lessons.keys():
                        if lesson_path in content:
                            # Extract context around the reference (100 chars before/after)
                            idx = content.find(lesson_path)
                            start = max(0, idx - 100)
                            end = min(len(content), idx + len(lesson_path) + 100)
                            context = content[start:end]

                            references.append(
                                LessonReference(
                                    lesson_path=lesson_path,
                                    conversation_id=conversation_id,
                                    timestamp=timestamp,
                                    context=context,
                                    role=role,
                                )
                            )

                    # Look for lesson title mentions
                    for lesson_path, (title, _, _) in lessons.items():
                        # Create pattern for title (case-insensitive, word boundaries)
                        # Skip very short titles to avoid false positives
                        if len(title) < 5:
                            continue

                        pattern = r"\b" + re.escape(title) + r"\b"
                        if re.search(pattern, content, re.IGNORECASE):
                            # Extract context
                            match = re.search(pattern, content, re.IGNORECASE)
                            if match:
                                idx = match.start()
                                start = max(0, idx - 100)
                                end = min(len(content), idx + len(title) + 100)
                                context = content[start:end]

                                references.append(
                                    LessonReference(
                                        lesson_path=lesson_path,
                                        conversation_id=conversation_id,
                                        timestamp=timestamp,
                                        context=context,
                                        role=role,
                                    )
                                )

                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"Error processing {conversation_file}: {e}")

    return references


def analyze_conversations(
    logs_dir: Path, lessons_dir: Path
) -> Tuple[Dict[str, LessonStats], List[LessonReference]]:
    """Analyze all conversations for lesson usage.

    Returns:
        Tuple of (lesson_stats, all_references)
    """
    console = Console()

    # Find all lessons
    console.print("[blue]Finding lessons...[/]")
    lessons = find_lesson_files(lessons_dir)
    console.print(f"Found {len(lessons)} lessons")

    # Initialize stats
    stats: Dict[str, LessonStats] = {}
    for lesson_path, (title, category, keywords) in lessons.items():
        stats[lesson_path] = LessonStats(
            path=lesson_path,
            title=title,
            category=category,
            keywords=keywords,
            reference_count=0,
            conversations=set(),
            last_referenced=None,
            contexts=[],
        )

    # Scan all conversations
    console.print("[blue]Scanning conversations...[/]")
    all_references = []

    conversation_dirs = [d for d in logs_dir.iterdir() if d.is_dir()]
    console.print(f"Found {len(conversation_dirs)} conversations")

    for conv_dir in conversation_dirs:
        conv_file = conv_dir / "conversation.jsonl"
        if not conv_file.exists():
            continue

        references = extract_lesson_references(conv_file, lessons)
        all_references.extend(references)

        # Update stats
        for ref in references:
            stat = stats[ref.lesson_path]
            stat.reference_count += 1
            stat.conversations.add(ref.conversation_id)
            stat.contexts.append(ref.context)

            if stat.last_referenced is None or ref.timestamp > stat.last_referenced:
                stat.last_referenced = ref.timestamp

    console.print(f"Found {len(all_references)} total lesson references")

    return stats, all_references


def generate_report(
    stats: Dict[str, LessonStats],
    references: List[LessonReference],
    output_file: Path | None = None,
):
    """Generate a comprehensive usage report.

    Args:
        stats: Dictionary of lesson statistics
        references: List of all lesson references
        output_file: Optional file to write report to
    """
    console = Console()

    # Sort lessons by usage
    sorted_stats = sorted(
        stats.values(),
        key=lambda s: (s.reference_count, len(s.conversations)),
        reverse=True,
    )

    # Create summary table
    table = Table(title="Lesson Usage Summary", show_lines=True)
    table.add_column("Rank", style="cyan", width=4)
    table.add_column("Lesson", style="bold")
    table.add_column("Category", style="yellow")
    table.add_column("References", justify="right", style="green")
    table.add_column("Conversations", justify="right", style="blue")
    table.add_column("Last Used", style="dim")

    for idx, stat in enumerate(sorted_stats[:20], 1):  # Top 20
        last_used = (
            stat.last_referenced.strftime("%Y-%m-%d")
            if stat.last_referenced
            else "Never"
        )
        table.add_row(
            str(idx),
            stat.title,
            stat.category,
            str(stat.reference_count),
            str(len(stat.conversations)),
            last_used,
        )

    console.print(table)

    # Show never-used lessons
    never_used = [s for s in sorted_stats if s.reference_count == 0]
    if never_used:
        console.print(f"\n[yellow]⚠️  {len(never_used)} lessons never referenced:[/]")
        for stat in never_used:
            console.print(f"  • {stat.category}/{stat.title}")

    # Category breakdown
    category_counts: Dict[str, int] = defaultdict(int)
    category_refs: Dict[str, int] = defaultdict(int)

    for stat in stats.values():
        category_counts[stat.category] += 1
        category_refs[stat.category] += stat.reference_count

    console.print("\n[bold]Category Breakdown:[/]")
    cat_table = Table()
    cat_table.add_column("Category", style="bold")
    cat_table.add_column("Lessons", justify="right")
    cat_table.add_column("References", justify="right")
    cat_table.add_column("Avg Refs/Lesson", justify="right")

    for category in sorted(category_counts.keys()):
        count = category_counts[category]
        refs = category_refs[category]
        avg = refs / count if count > 0 else 0
        cat_table.add_row(category, str(count), str(refs), f"{avg:.1f}")

    console.print(cat_table)

    # Keyword analysis
    keyword_counts: Dict[str, int] = defaultdict(int)
    keyword_refs: Dict[str, int] = defaultdict(int)
    keyword_lessons: Dict[str, List[str]] = defaultdict(list)

    for stat in stats.values():
        for keyword in stat.keywords:
            keyword_counts[keyword] += 1
            keyword_refs[keyword] += stat.reference_count
            if stat.reference_count > 0:
                keyword_lessons[keyword].append(stat.title)

    if keyword_counts:
        console.print("\n[bold]Keyword Analysis:[/]")
        kw_table = Table()
        kw_table.add_column("Keyword", style="bold")
        kw_table.add_column("Lessons", justify="right")
        kw_table.add_column("References", justify="right")
        kw_table.add_column("Avg Refs/Lesson", justify="right")

        # Sort by reference count descending
        sorted_keywords = sorted(
            keyword_counts.keys(), key=lambda k: keyword_refs[k], reverse=True
        )[:20]  # Top 20 keywords

        for keyword in sorted_keywords:
            count = keyword_counts[keyword]
            refs = keyword_refs[keyword]
            avg = refs / count if count > 0 else 0
            kw_table.add_row(keyword, str(count), str(refs), f"{avg:.1f}")

        console.print(kw_table)

        # Show keywords with no references
        unused_keywords = {kw for kw, refs in keyword_refs.items() if refs == 0}
        if unused_keywords:
            console.print(
                f"\n[yellow]ℹ️  {len(unused_keywords)} keywords never triggered:[/]"
            )
            for kw in sorted(unused_keywords)[:10]:  # Show first 10
                console.print(f"  • {kw}")

    # Recent activity
    recent_refs = sorted(references, key=lambda r: r.timestamp, reverse=True)[:10]
    if recent_refs:
        console.print("\n[bold]Recent Lesson References:[/]")
        for ref in recent_refs:
            lesson = stats[ref.lesson_path]
            console.print(
                f"  {ref.timestamp.strftime('%Y-%m-%d %H:%M')} - "
                f"{lesson.title} ({lesson.category})"
            )

    # Save detailed report if requested
    if output_file:
        with open(output_file, "w") as f:
            f.write("# Lesson Usage Report\n\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write("## Summary\n\n")
            f.write(f"- Total lessons: {len(stats)}\n")
            f.write(
                f"- Total references: {sum(s.reference_count for s in stats.values())}\n"
            )
            f.write(
                f"- Lessons used: {sum(1 for s in stats.values() if s.reference_count > 0)}\n"
            )
            f.write(f"- Never used: {len(never_used)}\n\n")

            f.write("## Most Used Lessons\n\n")
            for stat in sorted_stats[:20]:
                f.write(f"### {stat.title} ({stat.category})\n\n")
                f.write(f"- References: {stat.reference_count}\n")
                f.write(f"- Conversations: {len(stat.conversations)}\n")
                last_used = (
                    stat.last_referenced.strftime("%Y-%m-%d")
                    if stat.last_referenced
                    else "Never"
                )
                f.write(f"- Last used: {last_used}\n\n")

            f.write("## Never Used Lessons\n\n")
            for stat in never_used:
                f.write(f"- {stat.category}/{stat.title}\n")

        console.print(f"\n[green]✓ Detailed report saved to {output_file}[/]")


def main():
    """Main entry point."""
    console = Console()

    # Find paths
    repo_root = Path.cwd()
    while not (repo_root / ".git").exists() and repo_root != repo_root.parent:
        repo_root = repo_root.parent

    lessons_dir = repo_root / "lessons"
    logs_dir = Path.home() / ".local/share/gptme/logs"

    if not lessons_dir.exists():
        console.print(f"[red]Lessons directory not found: {lessons_dir}[/]")
        return

    if not logs_dir.exists():
        console.print(f"[red]Logs directory not found: {logs_dir}[/]")
        return

    console.print("[bold]Lesson Usage Analytics[/]\n")
    console.print(f"Lessons: {lessons_dir}")
    console.print(f"Logs: {logs_dir}\n")

    # Analyze conversations
    stats, references = analyze_conversations(logs_dir, lessons_dir)

    # Generate report
    output_file = repo_root / "knowledge" / "meta" / "lesson-usage-report.md"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    generate_report(stats, references, output_file)


if __name__ == "__main__":
    main()
