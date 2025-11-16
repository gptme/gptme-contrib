"""
Lesson Similarity Detection

Detects similar lessons to enable deduplication and clustering.
Uses text-based similarity (title + context) for fast comparison.
"""

from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Tuple


def extract_lesson_info(filepath: Path) -> Dict:
    """Extract key information from a lesson markdown file."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Extract title (first # heading)
    title = ""
    for line in content.split("\n"):
        if line.startswith("# "):
            title = line[2:].strip()
            break

    # Extract context section
    context = ""
    in_context = False
    for line in content.split("\n"):
        if line.startswith("## Context"):
            in_context = True
            continue
        elif line.startswith("##"):
            in_context = False
        elif in_context and line.strip():
            context += line.strip() + " "

    return {
        "filepath": filepath,
        "title": title,
        "context": context.strip(),
        "content": content,
    }


def text_similarity(text1: str, text2: str) -> float:
    """Compute normalized similarity between two text strings (0.0-1.0)."""
    if not text1 or not text2:
        return 0.0

    # Normalize: lowercase, strip whitespace
    text1 = text1.lower().strip()
    text2 = text2.lower().strip()

    # Use SequenceMatcher for edit distance-based similarity
    return SequenceMatcher(None, text1, text2).ratio()


def compute_similarity(lesson1: Dict, lesson2: Dict) -> float:
    """Compute similarity between two lessons (0.0-1.0).

    Weights:
    - Title: 60% (captures core lesson essence)
    - Context: 40% (provides additional detail)
    """
    title_sim = text_similarity(lesson1["title"], lesson2["title"])
    context_sim = text_similarity(lesson1["context"], lesson2["context"])

    # Weighted average
    return 0.6 * title_sim + 0.4 * context_sim


def find_similar_lessons(lesson_dir: Path, threshold: float = 0.7) -> List[List[Dict]]:
    """Find groups of similar lessons above the threshold.

    Returns list of similarity clusters, where each cluster is a list
    of similar lesson info dicts.

    Args:
        lesson_dir: Directory containing lesson markdown files
        threshold: Similarity threshold (0.0-1.0)

    Returns:
        List of clusters, e.g. [
            [lesson1, lesson2],  # similar group 1
            [lesson3, lesson4, lesson5],  # similar group 2
        ]
    """
    # Load all lessons
    lessons = []
    for filepath in lesson_dir.rglob("*.md"):
        if filepath.name.startswith("."):
            continue
        try:
            lesson_info = extract_lesson_info(filepath)
            lessons.append(lesson_info)
        except Exception as e:
            print(f"Warning: Failed to parse {filepath}: {e}")
            continue

    if len(lessons) < 2:
        return []

    # Compute pairwise similarities
    similarities: List[Tuple[int, int, float]] = []
    for i in range(len(lessons)):
        for j in range(i + 1, len(lessons)):
            sim = compute_similarity(lessons[i], lessons[j])
            if sim >= threshold:
                similarities.append((i, j, sim))

    # Group into clusters using union-find
    clusters: Dict[int, List[int]] = {}
    for i, j, sim in similarities:
        # Find clusters containing i and j
        cluster_i = None
        cluster_j = None
        for cluster_id, members in clusters.items():
            if i in members:
                cluster_i = cluster_id
            if j in members:
                cluster_j = cluster_id

        if cluster_i is None and cluster_j is None:
            # Create new cluster
            new_id = len(clusters)
            clusters[new_id] = [i, j]
        elif cluster_i is not None and cluster_j is None:
            # Add j to i's cluster
            clusters[cluster_i].append(j)
        elif cluster_i is None and cluster_j is not None:
            # Add i to j's cluster
            clusters[cluster_j].append(i)
        elif cluster_i is not None and cluster_j is not None and cluster_i != cluster_j:
            # Merge clusters
            clusters[cluster_i].extend(clusters[cluster_j])
            del clusters[cluster_j]

    # Convert to lesson info lists
    result = []
    for members in clusters.values():
        cluster = [lessons[idx] for idx in members]
        result.append(cluster)

    return result


def get_lesson_scores(lesson_info: Dict) -> Dict[str, float]:
    """Extract judge scores from lesson content if available.

    Returns empty dict if no scores found.
    """
    # TODO: Parse judge scores from lesson content or metadata
    # For now, return empty dict (implement when we have score storage)
    return {}


def select_best_lesson(cluster: List[Dict]) -> Dict:
    """Select the best lesson from a similarity cluster.

    Selection strategy:
    1. If judge scores available, pick highest average score
    2. Otherwise, pick most recent (by file modification time)

    Args:
        cluster: List of similar lesson info dicts

    Returns:
        The best lesson info dict
    """
    if not cluster:
        raise ValueError("Empty cluster")

    if len(cluster) == 1:
        return cluster[0]

    # Try to use judge scores
    scored_lessons = []
    for lesson in cluster:
        scores = get_lesson_scores(lesson)
        if scores:
            avg_score = sum(scores.values()) / len(scores)
            scored_lessons.append((avg_score, lesson))

    if scored_lessons:
        # Sort by score descending, return best
        scored_lessons.sort(key=lambda x: -x[0])
        return scored_lessons[0][1]

    # Fall back to most recent file
    cluster_with_mtime = [
        (lesson["filepath"].stat().st_mtime, lesson) for lesson in cluster
    ]
    cluster_with_mtime.sort(key=lambda x: -x[0])
    return cluster_with_mtime[0][1]


def check_against_existing_lessons(
    new_lesson: Dict,
    existing_lessons_dir: Path,
    threshold: float = 0.7,
) -> List[Dict]:
    """Check if a new lesson is similar to any existing lessons.

    Args:
        new_lesson: Lesson info dict (with title, context, filepath)
        existing_lessons_dir: Directory containing existing lessons (e.g., lessons/)
        threshold: Similarity threshold (0.0-1.0)

    Returns:
        List of similar existing lesson info dicts
    """
    similar_lessons = []

    # Scan all existing lessons
    for filepath in existing_lessons_dir.rglob("*.md"):
        if filepath.name.startswith(".") or "template" in filepath.name.lower():
            continue
        try:
            existing_lesson = extract_lesson_info(filepath)
            similarity = compute_similarity(new_lesson, existing_lesson)
            if similarity >= threshold:
                similar_lessons.append({**existing_lesson, "similarity": similarity})
        except Exception:
            # Skip files that can't be parsed
            continue

    # Sort by similarity descending
    similar_lessons.sort(key=lambda x: -x["similarity"])
    return similar_lessons


def deduplicate_lessons(
    lesson_dir: Path,
    archive_dir: Path,
    threshold: float = 0.7,
    dry_run: bool = False,
) -> Dict:
    """Deduplicate lessons by moving duplicates to archive.

    Args:
        lesson_dir: Directory containing lesson markdown files
        archive_dir: Directory to move duplicates to
        threshold: Similarity threshold (0.0-1.0)
        dry_run: If True, only report what would be done

    Returns:
        Dict with deduplication statistics
    """
    clusters = find_similar_lessons(lesson_dir, threshold)

    if not clusters:
        return {
            "clusters_found": 0,
            "lessons_kept": 0,
            "lessons_archived": 0,
            "clusters": [],
        }

    lessons_kept = []
    lessons_archived = []

    for cluster in clusters:
        # Select best lesson
        best = select_best_lesson(cluster)
        lessons_kept.append(best)

        # Archive the rest
        for lesson in cluster:
            if lesson["filepath"] != best["filepath"]:
                if not dry_run:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    dest = archive_dir / lesson["filepath"].name
                    lesson["filepath"].rename(dest)
                lessons_archived.append(lesson)

    return {
        "clusters_found": len(clusters),
        "lessons_kept": len(lessons_kept),
        "lessons_archived": len(lessons_archived),
        "clusters": [
            {
                "kept": best["filepath"].name,
                "archived": [
                    lesson["filepath"].name
                    for lesson in cluster
                    if lesson["filepath"] != best["filepath"]
                ],
            }
            for cluster, best in zip(clusters, lessons_kept)
        ],
    }
