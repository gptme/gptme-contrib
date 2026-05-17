"""Tests for lesson discovery recommendations."""

import json
import os
import time
from pathlib import Path

from gptme_lessons_extras.discovery import LessonDiscovery

LESSON_TEMPLATE = """\
---
match:
  keywords: [test]
status: active
---

# {title}

## Rule
Follow the rule.

## Pattern
```txt
example
```
"""


def _write_lesson(
    lessons_dir: Path, category: str, slug: str, title: str, mtime: float
) -> Path:
    lesson_path = lessons_dir / category / f"{slug}.md"
    lesson_path.parent.mkdir(parents=True, exist_ok=True)
    lesson_path.write_text(LESSON_TEMPLATE.format(title=title))
    os.utime(lesson_path, (mtime, mtime))
    return lesson_path


def _write_metrics(history_dir: Path, payload: dict) -> None:
    metrics_dir = history_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "network_metrics.json").write_text(json.dumps(payload))


def test_load_metrics_supports_current_metrics_cache_key(tmp_path: Path) -> None:
    lessons_dir = tmp_path / "lessons"
    history_dir = tmp_path / "history"
    now = time.time()

    _write_lesson(lessons_dir, "workflow", "preferred", "Preferred", now)

    _write_metrics(
        history_dir,
        {
            "metrics": {
                "preferred": {
                    "success_rate": 0.9,
                    "adoption_count": 5,
                }
            }
        },
    )

    discovery = LessonDiscovery(lessons_dir=lessons_dir, history_dir=history_dir)
    metrics = discovery.load_metrics()

    assert metrics["preferred"]["success_rate"] == 0.9
    assert metrics["preferred"]["adoption_count"] == 5


def test_load_metrics_keeps_legacy_lessons_cache_key(tmp_path: Path) -> None:
    lessons_dir = tmp_path / "lessons"
    history_dir = tmp_path / "history"
    now = time.time()

    _write_lesson(lessons_dir, "workflow", "legacy", "Legacy", now)

    _write_metrics(
        history_dir,
        {
            "lessons": {
                "legacy": {
                    "success_rate": 0.8,
                    "adoption_count": 2,
                }
            }
        },
    )

    discovery = LessonDiscovery(lessons_dir=lessons_dir, history_dir=history_dir)
    metrics = discovery.load_metrics()

    assert metrics["legacy"]["success_rate"] == 0.8
    assert metrics["legacy"]["adoption_count"] == 2


def test_recommend_prefers_fresher_lessons_when_other_scores_match(
    tmp_path: Path,
) -> None:
    lessons_dir = tmp_path / "lessons"
    history_dir = tmp_path / "history"
    now = time.time()
    stale = now - (400 * 24 * 60 * 60)

    _write_lesson(lessons_dir, "workflow", "fresh", "Fresh Lesson", now)
    _write_lesson(lessons_dir, "workflow", "stale", "Stale Lesson", stale)

    discovery = LessonDiscovery(lessons_dir=lessons_dir, history_dir=history_dir)
    recommendations = discovery.recommend(context="test", top_k=2)

    assert [rec.lesson_id for rec in recommendations] == ["fresh", "stale"]
    assert recommendations[0].recency_score > recommendations[1].recency_score
