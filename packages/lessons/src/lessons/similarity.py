#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "python-frontmatter>=1.0.0",
# ]
# ///

"""Lesson similarity and recency analysis.

Provides tools for:
- Finding similar lessons based on keywords, content, and structure
- Scoring lessons by recency (usage, modification, staleness)
- Identifying potential duplicates or lessons to merge
- Prioritizing lesson maintenance work

Example:
    Find similar lessons::

        $ python -m lessons.similarity --similar "lesson-name.md"

    Generate staleness report::

        $ python -m lessons.similarity --staleness
"""

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

import frontmatter


@dataclass
class SimilarityScore:
    """Similarity score between two lessons.

    Attributes:
        lesson1: Path to first lesson
        lesson2: Path to second lesson
        keyword_overlap: Ratio of shared keywords (0.0-1.0)
        title_similarity: Similarity of titles (0.0-1.0)
        category_match: Whether lessons are in same category
        total_score: Weighted combination of above metrics (0.0-1.0)
    """

    lesson1: str
    lesson2: str
    keyword_overlap: float
    title_similarity: float
    category_match: bool
    total_score: float


@dataclass
class RecencyScore:
    """Recency and staleness metrics for a lesson.

    Attributes:
        path: Path to lesson file
        last_modified: Last git commit date
        last_referenced: Last usage in conversations (from analytics)
        days_since_modified: Days since last modification
        days_since_referenced: Days since last reference (or None if never used)
        reference_count: Total references in conversations
        staleness_score: 0.0 (fresh) to 1.0 (stale)
        priority: "high", "medium", "low" priority for updates
    """

    path: str
    last_modified: datetime
    last_referenced: datetime | None
    days_since_modified: int
    days_since_referenced: int | None
    reference_count: int
    staleness_score: float
    priority: str


def extract_keywords(lesson_path: Path) -> Set[str]:
    """Extract keywords from lesson frontmatter.

    Args:
        lesson_path: Path to lesson file

    Returns:
        Set of keywords from match.keywords in frontmatter
    """
    try:
        with open(lesson_path) as f:
            post = frontmatter.load(f)
            if "match" in post.metadata and "keywords" in post.metadata["match"]:
                return set(post.metadata["match"]["keywords"])
    except Exception:
        pass
    return set()


def extract_title(lesson_path: Path) -> str:
    """Extract lesson title from first heading.

    Args:
        lesson_path: Path to lesson file

    Returns:
        Title string or filename if no heading found
    """
    try:
        with open(lesson_path) as f:
            post = frontmatter.load(f)
            match = re.search(r"^# (.+)$", post.content, re.MULTILINE)
            if match:
                return match.group(1)
    except Exception:
        pass
    return lesson_path.stem


def calculate_keyword_overlap(keywords1: Set[str], keywords2: Set[str]) -> float:
    """Calculate Jaccard similarity of keyword sets.

    Args:
        keywords1: First set of keywords
        keywords2: Second set of keywords

    Returns:
        Overlap ratio 0.0-1.0 (intersection / union)
    """
    if not keywords1 or not keywords2:
        return 0.0

    intersection = len(keywords1 & keywords2)
    union = len(keywords1 | keywords2)

    return intersection / union if union > 0 else 0.0


def calculate_title_similarity(title1: str, title2: str) -> float:
    """Calculate simple word overlap similarity between titles.

    Args:
        title1: First title
        title2: Second title

    Returns:
        Similarity ratio 0.0-1.0 based on word overlap
    """
    # Normalize: lowercase, remove punctuation, split into words
    words1 = set(re.findall(r"\w+", title1.lower()))
    words2 = set(re.findall(r"\w+", title2.lower()))

    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)

    return intersection / union if union > 0 else 0.0


def calculate_similarity(
    lesson1_path: Path, lesson2_path: Path, lessons_dir: Path
) -> SimilarityScore:
    """Calculate overall similarity between two lessons.

    Weights:
    - Keyword overlap: 50%
    - Title similarity: 30%
    - Category match: 20%

    Args:
        lesson1_path: Path to first lesson
        lesson2_path: Path to second lesson
        lessons_dir: Base lessons directory

    Returns:
        SimilarityScore object with metrics
    """
    # Extract metadata
    keywords1 = extract_keywords(lesson1_path)
    keywords2 = extract_keywords(lesson2_path)
    title1 = extract_title(lesson1_path)
    title2 = extract_title(lesson2_path)

    # Get categories
    cat1 = lesson1_path.relative_to(lessons_dir).parts[0]
    cat2 = lesson2_path.relative_to(lessons_dir).parts[0]
    category_match = cat1 == cat2

    # Calculate component similarities
    keyword_sim = calculate_keyword_overlap(keywords1, keywords2)
    title_sim = calculate_title_similarity(title1, title2)
    category_sim = 1.0 if category_match else 0.0

    # Weighted total (keyword=50%, title=30%, category=20%)
    total = (keyword_sim * 0.5) + (title_sim * 0.3) + (category_sim * 0.2)

    rel_path1 = str(lesson1_path.relative_to(lessons_dir))
    rel_path2 = str(lesson2_path.relative_to(lessons_dir))

    return SimilarityScore(
        lesson1=rel_path1,
        lesson2=rel_path2,
        keyword_overlap=keyword_sim,
        title_similarity=title_sim,
        category_match=category_match,
        total_score=total,
    )


def find_similar_lessons(
    lesson_path: Path,
    lessons_dir: Path,
    min_similarity: float = 0.3,
    max_results: int = 10,
) -> List[SimilarityScore]:
    """Find lessons similar to the given lesson.

    Args:
        lesson_path: Path to lesson to compare against
        lessons_dir: Base lessons directory
        min_similarity: Minimum similarity score to include (0.0-1.0)
        max_results: Maximum number of results to return

    Returns:
        List of SimilarityScore objects, sorted by total_score descending
    """
    similarities = []

    # Compare against all other lessons
    for other_path in lessons_dir.rglob("*.md"):
        if other_path.name in ("README.md", "TODO.md", "lesson-template.md"):
            continue
        if other_path == lesson_path:
            continue

        score = calculate_similarity(lesson_path, other_path, lessons_dir)
        if score.total_score >= min_similarity:
            similarities.append(score)

    # Sort by total score descending
    similarities.sort(key=lambda x: x.total_score, reverse=True)

    return similarities[:max_results]


def get_last_modified_date(file_path: Path) -> datetime:
    """Get last git commit date for a file.

    Args:
        file_path: Path to file

    Returns:
        Datetime of last commit, or file mtime if git fails
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI", str(file_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        date_str = result.stdout.strip()
        if date_str:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (subprocess.CalledProcessError, ValueError):
        pass

    # Fallback to file mtime
    return datetime.fromtimestamp(file_path.stat().st_mtime).astimezone()


def calculate_staleness_score(
    days_since_modified: int,
    days_since_referenced: int | None,
    reference_count: int,
) -> float:
    """Calculate staleness score for a lesson.

    Scoring logic:
    - Very fresh (0-30 days): 0.0-0.2
    - Fresh (30-90 days): 0.2-0.4
    - Aging (90-180 days): 0.4-0.6
    - Stale (180-365 days): 0.6-0.8
    - Very stale (365+ days): 0.8-1.0

    Adjustments:
    - High usage (10+ refs) reduces staleness by 20%
    - Never referenced increases staleness by 20%
    - Recently referenced (30 days) reduces staleness by 10%

    Args:
        days_since_modified: Days since last git commit
        days_since_referenced: Days since last conversation reference (None if never)
        reference_count: Total references in conversations

    Returns:
        Staleness score 0.0 (fresh) to 1.0 (very stale)
    """
    # Base score from modification age
    if days_since_modified < 30:
        base_score = 0.1
    elif days_since_modified < 90:
        base_score = 0.3
    elif days_since_modified < 180:
        base_score = 0.5
    elif days_since_modified < 365:
        base_score = 0.7
    else:
        base_score = 0.9

    # Adjust for usage
    if reference_count >= 10:
        base_score -= 0.2  # High usage keeps it fresh
    elif days_since_referenced is None:
        base_score += 0.2  # Never referenced = stale
    elif days_since_referenced < 30:
        base_score -= 0.1  # Recently referenced = fresh

    # Clamp to 0.0-1.0
    return max(0.0, min(1.0, base_score))


def calculate_recency_scores(
    lessons_dir: Path, usage_stats: Dict[str, Tuple[datetime | None, int]] | None = None
) -> List[RecencyScore]:
    """Calculate recency scores for all lessons.

    Args:
        lessons_dir: Base lessons directory
        usage_stats: Optional dict mapping lesson path to (last_referenced, reference_count)
                    from analytics. If None, only git history is used.

    Returns:
        List of RecencyScore objects
    """
    scores = []
    now = datetime.now().astimezone()

    for lesson_path in lessons_dir.rglob("*.md"):
        if lesson_path.name in ("README.md", "TODO.md", "lesson-template.md"):
            continue

        # Get git modification date
        last_modified = get_last_modified_date(lesson_path)
        days_since_modified = (now - last_modified).days

        # Get usage stats if provided
        rel_path = str(lesson_path.relative_to(lessons_dir))
        last_referenced = None
        reference_count = 0
        days_since_referenced = None

        if usage_stats and rel_path in usage_stats:
            last_referenced, reference_count = usage_stats[rel_path]
            if last_referenced:
                days_since_referenced = (now - last_referenced).days

        # Calculate staleness
        staleness = calculate_staleness_score(
            days_since_modified, days_since_referenced, reference_count
        )

        # Determine priority
        if staleness >= 0.7:
            priority = "high"
        elif staleness >= 0.4:
            priority = "medium"
        else:
            priority = "low"

        scores.append(
            RecencyScore(
                path=rel_path,
                last_modified=last_modified,
                last_referenced=last_referenced,
                days_since_modified=days_since_modified,
                days_since_referenced=days_since_referenced,
                reference_count=reference_count,
                staleness_score=staleness,
                priority=priority,
            )
        )

    return scores


def find_duplicates(
    lessons_dir: Path, similarity_threshold: float = 0.7
) -> List[Tuple[str, str, float]]:
    """Find potential duplicate lessons based on high similarity.

    Args:
        lessons_dir: Base lessons directory
        similarity_threshold: Minimum similarity to consider duplicates (0.0-1.0)

    Returns:
        List of (lesson1, lesson2, similarity_score) tuples
    """
    duplicates = []
    lessons = list(lessons_dir.rglob("*.md"))
    lessons = [
        lesson
        for lesson in lessons
        if lesson.name not in ("README.md", "TODO.md", "lesson-template.md")
    ]

    # Compare all pairs
    for i, lesson1 in enumerate(lessons):
        for lesson2 in lessons[i + 1 :]:
            score = calculate_similarity(lesson1, lesson2, lessons_dir)
            if score.total_score >= similarity_threshold:
                duplicates.append((score.lesson1, score.lesson2, score.total_score))

    # Sort by similarity descending
    duplicates.sort(key=lambda x: x[2], reverse=True)

    return duplicates


if __name__ == "__main__":
    import sys

    workspace_root = Path(__file__).parent.parent.parent.parent.parent
    lessons_dir = workspace_root / "lessons"

    if not lessons_dir.exists():
        print(f"Error: Lessons directory not found: {lessons_dir}")
        sys.exit(1)

    if len(sys.argv) > 1 and sys.argv[1] == "--similar":
        if len(sys.argv) < 3:
            print("Usage: similarity.py --similar <lesson-name>")
            sys.exit(1)

        lesson_name = sys.argv[2]
        lesson_path = None

        # Find lesson by name
        for path in lessons_dir.rglob("*.md"):
            if path.name == lesson_name or path.stem == lesson_name:
                lesson_path = path
                break

        if not lesson_path:
            print(f"Error: Lesson not found: {lesson_name}")
            sys.exit(1)

        print(f"\nFinding lessons similar to: {lesson_path.name}")
        print("=" * 60)

        similar = find_similar_lessons(lesson_path, lessons_dir)

        if not similar:
            print("\nNo similar lessons found (threshold: 0.3)")
        else:
            for score in similar:
                print(f"\n{score.lesson2}")
                print(f"  Total similarity: {score.total_score:.2f}")
                print(f"  Keyword overlap: {score.keyword_overlap:.2f}")
                print(f"  Title similarity: {score.title_similarity:.2f}")
                print(f"  Same category: {score.category_match}")

    elif len(sys.argv) > 1 and sys.argv[1] == "--staleness":
        print("\nCalculating lesson staleness scores...")
        print("=" * 60)

        recency_scores = calculate_recency_scores(lessons_dir)
        recency_scores.sort(key=lambda x: x.staleness_score, reverse=True)

        # Show top 20 stale lessons
        print("\nTop 20 Stale Lessons (High Priority for Update):")
        for recency_score in recency_scores[:20]:
            print(f"\n{recency_score.path}")
            print(
                f"  Staleness: {recency_score.staleness_score:.2f} ({recency_score.priority} priority)"
            )
            print(f"  Last modified: {recency_score.days_since_modified} days ago")
            if recency_score.days_since_referenced is not None:
                print(
                    f"  Last referenced: {recency_score.days_since_referenced} days ago"
                )
            else:
                print("  Last referenced: Never")
            print(f"  References: {recency_score.reference_count}")

    elif len(sys.argv) > 1 and sys.argv[1] == "--duplicates":
        print("\nFinding potential duplicate lessons...")
        print("=" * 60)

        duplicates = find_duplicates(lessons_dir)

        if not duplicates:
            print("\nNo potential duplicates found (threshold: 0.7)")
        else:
            print(f"\nFound {len(duplicates)} potential duplicates:\n")
            for lesson1, lesson2, similarity in duplicates:
                print(f"{lesson1}")
                print(f"  vs {lesson2}")
                print(f"  Similarity: {similarity:.2f}\n")

    else:
        print("Usage:")
        print("  similarity.py --similar <lesson-name>  # Find similar lessons")
        print("  similarity.py --staleness              # Show stale lessons")
        print("  similarity.py --duplicates             # Find potential duplicates")
