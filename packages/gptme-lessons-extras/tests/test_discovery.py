import os
from pathlib import Path

from gptme_lessons_extras.discovery import LessonDiscovery


def _write_lesson(path: Path, keyword: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                "match:",
                "  keywords: [" + keyword + "]",
                "---",
                "",
                "# Test Lesson",
                "",
                "## Rule",
                "Do the thing.",
                "",
                "## Pattern",
                "Use the tool.",
                "",
            ]
        )
    )


def test_load_metrics_supports_current_cache_format(tmp_path: Path) -> None:
    history_dir = tmp_path / ".lessons-history"
    metrics_dir = history_dir / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "network_metrics.json").write_text(
        """
{
  "timestamp": "2026-05-17T00:00:00+00:00",
  "lesson_count": 1,
  "metrics": {
    "alpha": {
      "success_rate": 0.9,
      "adoption_count": 4
    }
  }
}
""".strip()
    )

    discovery = LessonDiscovery(
        lessons_dir=tmp_path / "lessons", history_dir=history_dir
    )

    assert discovery.load_metrics() == {
        "alpha": {"success_rate": 0.9, "adoption_count": 4}
    }


def test_recommend_prefers_more_recent_lesson_when_other_scores_tie(
    tmp_path: Path,
) -> None:
    lessons_dir = tmp_path / "lessons"
    history_dir = tmp_path / ".lessons-history"
    metrics_dir = history_dir / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "network_metrics.json").write_text(
        """
{
  "metrics": {
    "recent": {"success_rate": 0.5, "adoption_count": 0},
    "stale": {"success_rate": 0.5, "adoption_count": 0}
  }
}
""".strip()
    )

    recent = lessons_dir / "workflow" / "recent.md"
    stale = lessons_dir / "workflow" / "stale.md"
    _write_lesson(recent, "shell")
    _write_lesson(stale, "shell")

    now = int(os.path.getmtime(recent))
    os.utime(stale, (now - 200 * 24 * 60 * 60, now - 200 * 24 * 60 * 60))

    discovery = LessonDiscovery(lessons_dir=lessons_dir, history_dir=history_dir)
    results = discovery.recommend(keywords=["shell"], top_k=2)

    assert [result.lesson_id for result in results] == ["recent", "stale"]
    assert results[0].recency_score > results[1].recency_score
    assert results[0].success_score == results[1].success_score == 0.5
