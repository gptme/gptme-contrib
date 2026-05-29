"""Tests for the regex-based LLM smell detector."""

from __future__ import annotations

from pathlib import Path

from gptme_sessions.smell import compute_smell_score, detect_smells

CLEAN_PROSE = (
    "Fixed the off-by-one in the pagination cursor. The endpoint scanned the "
    "whole JSONL file on every request, so I cached the offset index and added "
    "a regression test. CI is green and the latency dropped from 400ms to 12ms."
)

SMELLY_PROSE = (
    "It's worth noting that this is a testament to our ever-evolving tapestry "
    "of solutions. Let's delve into the realm of possibilities. It's not just a "
    "feature, it's a game-changer. In conclusion, I'd be happy to help further. "
    "Great question! Moreover, this showcases a comprehensive approach."
)


def test_detect_smells_structure():
    """detect_smells returns the documented keys."""
    report = detect_smells(CLEAN_PROSE)
    for key in (
        "word_count",
        "total_hits",
        "weighted_score",
        "em_dash_count",
        "em_dash_per_1k",
        "by_category",
        "hits",
    ):
        assert key in report
    assert report["word_count"] > 0
    assert isinstance(report["hits"], list)
    assert isinstance(report["by_category"], dict)


def test_clean_prose_scores_low():
    """Normal technical prose should produce few or no weighted hits."""
    report = detect_smells(CLEAN_PROSE)
    # "robust"/"leverage" etc. are absent here; expect a near-zero score.
    assert report["weighted_score"] == 0.0
    assert report["total_hits"] == 0


def test_smelly_prose_detected():
    """Text packed with tells should score well above clean prose."""
    clean = detect_smells(CLEAN_PROSE)
    smelly = detect_smells(SMELLY_PROSE)
    assert smelly["total_hits"] > clean["total_hits"]
    assert smelly["weighted_score"] > clean["weighted_score"]
    assert smelly["weighted_score"] > 0


def test_specific_tells_caught():
    """High-confidence tells fire on their trigger phrases."""
    labels = {h["label"] for h in detect_smells(SMELLY_PROSE)["hits"]}
    assert "delve" in labels
    assert "tapestry" in labels
    assert "it's not just X, it's Y" in labels
    assert "it's worth noting" in labels


def test_by_category_aggregation():
    """by_category counts match the sum of per-label hit counts."""
    report = detect_smells(SMELLY_PROSE)
    for cat, total in report["by_category"].items():
        per_label = sum(h["count"] for h in report["hits"] if h["category"] == cat)
        assert per_label == total


def test_em_dash_abuse():
    """Excess em-dashes register once past the tolerated density."""
    heavy = "Word — word — word — word — word — word — word — word — word."
    report = detect_smells(heavy)
    assert report["em_dash_count"] >= 8
    assert any(h["category"] == "em_dash" for h in report["hits"])


def test_empty_text():
    """Empty input does not divide by zero."""
    report = detect_smells("")
    assert report["word_count"] == 0
    assert report["weighted_score"] == 0.0
    assert report["total_hits"] == 0


def test_compute_smell_score_range(tmp_path: Path):
    """compute_smell_score returns a value in [0, 1] for real files."""
    clean_file = tmp_path / "clean.md"
    clean_file.write_text(CLEAN_PROSE, encoding="utf-8")
    smelly_file = tmp_path / "smelly.md"
    smelly_file.write_text(SMELLY_PROSE, encoding="utf-8")

    clean_score = compute_smell_score(clean_file)
    smelly_score = compute_smell_score(smelly_file)

    assert clean_score is not None and smelly_score is not None
    assert 0.0 <= clean_score <= 1.0
    assert 0.0 <= smelly_score <= 1.0
    assert smelly_score > clean_score


def test_compute_smell_score_missing_file(tmp_path: Path):
    """A non-existent path returns None rather than raising."""
    assert compute_smell_score(tmp_path / "does-not-exist.md") is None


def test_compute_smell_score_empty_file(tmp_path: Path):
    """An empty/whitespace file returns None."""
    empty = tmp_path / "empty.md"
    empty.write_text("   \n  \n", encoding="utf-8")
    assert compute_smell_score(empty) is None
