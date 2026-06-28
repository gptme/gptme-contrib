"""Tests for lesson discovery recommendation scoring."""

import json
import os
import time
from pathlib import Path

from gptme_lessons_extras.discovery import LessonDiscovery

LESSON_TEMPLATE = """---
match:
  keywords: [shell]
---
# {title}

## Rule
Use the shell.

## Pattern
Run the command.
"""


def _write_lesson(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LESSON_TEMPLATE.format(title=title))


def test_load_metrics_supports_current_network_format(tmp_path: Path) -> None:
    """Discovery should load the metrics key written by the aggregator."""
    history_dir = tmp_path / ".lessons-history"
    metrics_dir = history_dir / "metrics"
    metrics_dir.mkdir(parents=True)
    expected = {
        "demo": {
            "success_rate": 0.9,
            "adoption_count": 3,
            "last_updated": "2026-05-17T00:00:00+00:00",
        }
    }
    (metrics_dir / "network_metrics.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-05-17T00:00:00+00:00",
                "lesson_count": 1,
                "metrics": expected,
            }
        )
    )

    discovery = LessonDiscovery(history_dir=history_dir)

    assert discovery.load_metrics() == expected


def test_recommend_uses_loaded_metrics_scores(tmp_path: Path) -> None:
    """Success and adoption scores should reflect the current metrics format."""
    lessons_dir = tmp_path / "lessons" / "tools"
    strong = lessons_dir / "strong.md"
    weak = lessons_dir / "weak.md"
    _write_lesson(strong, "Strong")
    _write_lesson(weak, "Weak")

    shared_time = time.time() - 30 * 24 * 3600
    os.utime(strong, (shared_time, shared_time))
    os.utime(weak, (shared_time, shared_time))

    history_dir = tmp_path / ".lessons-history"
    metrics_dir = history_dir / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "network_metrics.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-05-17T00:00:00+00:00",
                "lesson_count": 2,
                "metrics": {
                    "strong": {
                        "success_rate": 1.0,
                        "adoption_count": 5,
                        "last_updated": "2026-05-01T00:00:00+00:00",
                    },
                    "weak": {
                        "success_rate": 0.0,
                        "adoption_count": 0,
                        "last_updated": "2026-05-01T00:00:00+00:00",
                    },
                },
            }
        )
    )

    discovery = LessonDiscovery(
        lessons_dir=tmp_path / "lessons", history_dir=history_dir
    )

    recommendations = discovery.recommend(keywords=["shell"], top_k=2)

    assert [rec.lesson_id for rec in recommendations] == ["strong", "weak"]
    assert recommendations[0].success_score == 1.0
    assert recommendations[0].adoption_score == 1.0
    assert recommendations[1].success_score == 0.0
    assert recommendations[1].adoption_score == 0.0


def test_recommend_excludes_non_lesson_files(tmp_path: Path) -> None:
    """README, TODO, and lesson-template must not be scored as real lessons."""
    lessons_dir = tmp_path / "lessons"
    _write_lesson(lessons_dir / "tools" / "real.md", "Real")
    # Non-lesson files that share the lesson keyword shape and would otherwise
    # be picked up by rglob("*.md") and scored as lessons.
    _write_lesson(lessons_dir / "templates" / "lesson-template.md", "Template")
    _write_lesson(lessons_dir / "TODO.md", "Todo")
    _write_lesson(lessons_dir / "README.md", "Readme")

    discovery = LessonDiscovery(
        lessons_dir=lessons_dir, history_dir=tmp_path / ".lessons-history"
    )

    ids = [rec.lesson_id for rec in discovery.recommend(keywords=["shell"], top_k=10)]

    assert "real" in ids
    assert "lesson-template" not in ids
    assert "TODO" not in ids
    assert "README" not in ids


def test_find_similar_excludes_non_lesson_files(tmp_path: Path) -> None:
    """Non-lesson files must not appear in find_similar results."""
    lessons_dir = tmp_path / "lessons"
    # Target lesson
    _write_lesson(lessons_dir / "tools" / "reference.md", "Reference")
    # A similar real lesson (same template → high text similarity)
    _write_lesson(lessons_dir / "tools" / "similar.md", "Similar")
    # Non-lesson files sharing the lesson directory
    _write_lesson(lessons_dir / "templates" / "lesson-template.md", "Template")
    _write_lesson(lessons_dir / "TODO.md", "Todo")
    _write_lesson(lessons_dir / "README.md", "Readme")

    discovery = LessonDiscovery(
        lessons_dir=lessons_dir, history_dir=tmp_path / ".lessons-history"
    )

    results = discovery.find_similar("reference", threshold=0.0)

    result_ids = [r.lesson_b for r in results]
    assert "similar" in result_ids
    assert "lesson-template" not in result_ids
    assert "TODO" not in result_ids
    assert "README" not in result_ids


def test_find_all_duplicates_excludes_non_lesson_files(tmp_path: Path) -> None:
    """Non-lesson files must not appear in find_all_duplicates results."""
    lessons_dir = tmp_path / "lessons"
    # Two lessons with identical template → high text similarity → duplicate
    _write_lesson(lessons_dir / "tools" / "dup-a.md", "DupA")
    _write_lesson(lessons_dir / "tools" / "dup-b.md", "DupB")
    # Non-lesson files
    _write_lesson(lessons_dir / "templates" / "lesson-template.md", "Template")
    _write_lesson(lessons_dir / "TODO.md", "Todo")
    _write_lesson(lessons_dir / "README.md", "Readme")

    discovery = LessonDiscovery(
        lessons_dir=lessons_dir, history_dir=tmp_path / ".lessons-history"
    )

    results = discovery.find_all_duplicates(threshold=0.0)

    # At least one real-lesson pair should be found
    assert len(results) >= 1
    result_ids = set()
    for a, b, _ in results:
        result_ids.add(a)
        result_ids.add(b)
    assert "dup-a" in result_ids
    assert "dup-b" in result_ids
    assert "lesson-template" not in result_ids
    assert "TODO" not in result_ids
    assert "README" not in result_ids


def test_recommend_uses_file_recency_when_metrics_are_missing(tmp_path: Path) -> None:
    """Recent files should get a freshness boost even without metrics data."""
    lessons_dir = tmp_path / "lessons" / "tools"
    recent = lessons_dir / "recent.md"
    old = lessons_dir / "old.md"
    _write_lesson(recent, "Recent")
    _write_lesson(old, "Old")

    now = time.time()
    recent_time = now - 5 * 24 * 3600
    old_time = now - 400 * 24 * 3600
    os.utime(recent, (recent_time, recent_time))
    os.utime(old, (old_time, old_time))

    discovery = LessonDiscovery(
        lessons_dir=tmp_path / "lessons", history_dir=tmp_path / ".lessons-history"
    )

    recommendations = discovery.recommend(keywords=["shell"], top_k=2)

    assert [rec.lesson_id for rec in recommendations] == ["recent", "old"]
    assert recommendations[0].recency_score > recommendations[1].recency_score
    assert recommendations[0].recency_score > 0.0
    assert recommendations[1].recency_score == 0.0
