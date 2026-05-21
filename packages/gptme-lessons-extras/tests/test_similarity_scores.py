"""Tests for lesson similarity score parsing."""

from pathlib import Path
from textwrap import dedent

import pytest
from gptme_lessons_extras.utils.similarity import (
    extract_lesson_info,
    get_lesson_scores,
    select_best_lesson,
)


def _write_lesson(tmp_path: Path, filename: str, content: str) -> Path:
    """Write a lesson file and return its path."""
    filepath = tmp_path / filename
    filepath.write_text(dedent(content))
    return filepath


def _info(filepath: Path) -> dict:
    """Extract lesson info from a filepath."""
    info = extract_lesson_info(filepath)
    return info


class TestGetLessonScores:
    """Tests for get_lesson_scores() across supported score formats."""

    def test_no_scores_in_plain_markdown(self, tmp_path):
        """Returns empty dict when no scores are present."""
        fp = _write_lesson(
            tmp_path,
            "plain.md",
            """\
            # Plain Lesson

            ## Context
            No scores here.

            ## Rule
            Do the right thing.
            """,
        )
        info = _info(fp)
        assert get_lesson_scores(info) == {}

    def test_direct_scores_block(self, tmp_path):
        """Parses a top-level `scores:` frontmatter block."""
        fp = _write_lesson(
            tmp_path,
            "scored.md",
            """\
            ---
            scores:
              correctness: 0.85
              specificity: 0.70
              brevity: 0.90
            ---
            # Scored Lesson

            ## Context
            Testing.
            """,
        )
        info = _info(fp)
        result = get_lesson_scores(info)
        assert result == {"correctness": 0.85, "specificity": 0.70, "brevity": 0.90}

    def test_quality_nested_scores_block(self, tmp_path):
        """Parses a `quality.scores:` nested frontmatter block."""
        fp = _write_lesson(
            tmp_path,
            "quality-scored.md",
            """\
            ---
            quality:
              scores:
                correctness: 0.92
                evidence_use: 0.78
              other: meta
            ---
            # Quality-Scored Lesson

            ## Context
            Nested.
            """,
        )
        info = _info(fp)
        result = get_lesson_scores(info)
        assert result == {"correctness": 0.92, "evidence_use": 0.78}

    def test_effectiveness_average(self, tmp_path):
        """Parses an `effectiveness.average:` LOO-style frontmatter block."""
        fp = _write_lesson(
            tmp_path,
            "effective.md",
            """\
            ---
            effectiveness:
              average: 0.76
              samples: 42
            ---
            # Effective Lesson

            ## Context
            LOO-style.
            """,
        )
        info = _info(fp)
        result = get_lesson_scores(info)
        assert result == {"effectiveness": 0.76}

    def test_effectiveness_direct_numeric(self, tmp_path):
        """Parses an `effectiveness:` direct numeric frontmatter value."""
        fp = _write_lesson(
            tmp_path,
            "eff-num.md",
            """\
            ---
            effectiveness: 0.65
            ---
            # Effective Num

            ## Context
            Direct.
            """,
        )
        info = _info(fp)
        result = get_lesson_scores(info)
        assert result == {"effectiveness": 0.65}

    def test_non_numeric_scores_are_ignored(self, tmp_path):
        """Non-numeric score values are dropped, not treated as zero."""
        fp = _write_lesson(
            tmp_path,
            "mixed.md",
            """\
            ---
            scores:
              correctness: 0.80
              rationale: "good"
              specificity: high
              brevity: 0.95
            ---
            # Mixed Lesson

            ## Context
            Mixed types.
            """,
        )
        info = _info(fp)
        result = get_lesson_scores(info)
        assert result == {"correctness": 0.80, "brevity": 0.95}

    def test_boolean_values_in_scores_are_ignored(self, tmp_path):
        """Boolean values in scores are ignored (bool subclass of int trap)."""
        fp = _write_lesson(
            tmp_path,
            "bool-scores.md",
            """\
            ---
            scores:
              correctness: 0.80
              is_validated: true
              brevity: 0.90
              active: false
            ---
            # Boolean-in-Scores Lesson

            ## Context
            Should not coerce bool to 1.0/0.0.
            """,
        )
        info = _info(fp)
        result = get_lesson_scores(info)
        # Only numeric values should survive; bool values are dropped
        assert result == {"correctness": 0.80, "brevity": 0.90}
        # Verify average is not skewed by bool -> 1.0 on is_validated
        avg = sum(result.values()) / len(result)
        assert avg == pytest.approx(
            0.85
        )  # (0.80 + 0.90) / 2, not (0.80 + 1.0 + 0.90 + 0.0) / 4

    def test_empty_scores_block_returns_empty(self, tmp_path):
        """A `scores:` block with no numeric children returns empty."""
        fp = _write_lesson(
            tmp_path,
            "empty-scores.md",
            """\
            ---
            scores:
              comment: "all qualitative"
            ---
            # Empty Scores

            ## Context
            No numerics.
            """,
        )
        info = _info(fp)
        assert get_lesson_scores(info) == {}

    def test_malformed_frontmatter_returns_empty(self, tmp_path):
        """Malformed YAML frontmatter returns empty, does not crash."""
        fp = _write_lesson(
            tmp_path,
            "malformed.md",
            """\
            ---
            scores: [unclosed
            ---
            # Broken
            """,
        )
        info = _info(fp)
        # Should not raise
        result = get_lesson_scores(info)
        assert result == {}

    def test_no_frontmatter_returns_empty(self, tmp_path):
        """Content with no YAML frontmatter at all returns empty."""
        fp = _write_lesson(
            tmp_path,
            "no-fm.md",
            """\
            # No Frontmatter

            Just content.
            """,
        )
        info = _info(fp)
        assert get_lesson_scores(info) == {}

    def test_integer_scores_coerced_to_float(self, tmp_path):
        """Integer score values are coerced to float."""
        fp = _write_lesson(
            tmp_path,
            "ints.md",
            """\
            ---
            scores:
              correctness: 1
              brevity: 0
            ---
            # Int Scores
            """,
        )
        info = _info(fp)
        result = get_lesson_scores(info)
        assert result == {"correctness": 1.0, "brevity": 0.0}
        assert isinstance(result["correctness"], float)


class TestSelectBestLesson:
    """Tests for select_best_lesson() with and without scores."""

    def test_selects_highest_scored_lesson(self, tmp_path):
        """When multiple lessons have scores, the highest average wins."""
        scored_content = """\
            ---
            scores:
              correctness: 0.95
              brevity: 0.90
            ---
            # Best

            ## Context
            High score.
            """
        fp_best = _write_lesson(tmp_path, "best.md", scored_content)

        mid_content = """\
            ---
            scores:
              correctness: 0.70
              brevity: 0.60
            ---
            # Mid

            ## Context
            Medium score.
            """
        fp_mid = _write_lesson(tmp_path, "mid.md", mid_content)

        cluster = [_info(fp_mid), _info(fp_best)]
        chosen = select_best_lesson(cluster)
        assert chosen["title"] == "Best"

    def test_falls_back_to_mtime_when_no_scores(self, tmp_path):
        """When no lesson has scores, falls back to most recent mtime."""
        older = _write_lesson(
            tmp_path,
            "older.md",
            """\
            # Older
            ## Context
            No scores.
            """,
        )
        newer = _write_lesson(
            tmp_path,
            "newer_lesson.md",
            """\
            # Newer
            ## Context
            No scores.
            """,
        )
        # Ensure mtime ordering
        newer.touch()  # touch to make newer the most recent

        cluster = [_info(older), _info(newer)]
        chosen = select_best_lesson(cluster)
        assert chosen["title"] == "Newer"

    def test_score_beats_mtime(self, tmp_path):
        """A lower-mtime lesson with a high score beats a newer no-score lesson."""
        scored_content = """\
            ---
            scores:
              correctness: 0.99
            ---
            # Scored
            ## Context
            Has score.
            """
        fp_scored = _write_lesson(tmp_path, "scored.md", scored_content)

        fp_unscored = _write_lesson(
            tmp_path,
            "unscored_newer.md",
            """\
            # Unscored But Newer
            ## Context
            No scores.
            """,
        )
        fp_unscored.touch()  # make it newer

        cluster = [_info(fp_unscored), _info(fp_scored)]
        chosen = select_best_lesson(cluster)
        assert chosen["title"] == "Scored"

    def test_single_lesson_cluster(self, tmp_path):
        """Cluster with a single lesson returns it directly."""
        fp = _write_lesson(
            tmp_path,
            "solo.md",
            """\
            # Solo
            ## Context
            Alone.
            """,
        )
        cluster = [_info(fp)]
        chosen = select_best_lesson(cluster)
        assert chosen["title"] == "Solo"
