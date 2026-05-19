"""Tests for lesson generation duplicate pre-checks."""

import json
from pathlib import Path

from gptme_lessons_extras.generate import generate_lessons_with_evolution


def _write_analysis(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "conversation_id": "conv-123",
                "experiences": [
                    {
                        "title": "Duplicate-prone lesson",
                        "context": "Repeated mistake context",
                        "confidence": 0.9,
                    }
                ],
            }
        )
    )


def test_generate_lessons_with_evolution_skips_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    """Duplicate pre-check should stop GEPA generation when skipping is enabled."""
    analysis_file = tmp_path / "analysis.json"
    _write_analysis(analysis_file)
    existing_lessons_dir = tmp_path / "lessons"
    existing_lessons_dir.mkdir()

    def fail_if_called(**_kwargs):
        raise AssertionError("GEPA should not run when duplicate pre-check skips")

    monkeypatch.setattr(
        "gptme_lessons_extras.generate.gepa_lite_evolve",
        fail_if_called,
    )
    monkeypatch.setattr(
        "gptme_lessons_extras.utils.similarity.check_against_existing_lessons",
        lambda *_args, **_kwargs: [
            {
                "title": "Existing duplicate",
                "filepath": existing_lessons_dir / "patterns" / "existing.md",
                "similarity": 0.91,
            }
        ],
    )

    generated = generate_lessons_with_evolution(
        analysis_file=analysis_file,
        output_dir=tmp_path / "output",
        existing_lessons_dir=existing_lessons_dir,
        verbose=False,
    )

    assert generated == []


def test_generate_lessons_with_evolution_warns_only_when_duplicates_allowed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Warn-only mode should still run GEPA and save the generated lesson."""
    analysis_file = tmp_path / "analysis.json"
    _write_analysis(analysis_file)
    existing_lessons_dir = tmp_path / "lessons"
    existing_lessons_dir.mkdir()
    output_dir = tmp_path / "output"

    monkeypatch.setattr(
        "gptme_lessons_extras.generate.gepa_lite_evolve",
        lambda **_kwargs: (
            "generated lesson markdown",
            {"scores": {"quality": 0.9, "specificity": 0.9}},
            [],
        ),
    )
    monkeypatch.setattr(
        "gptme_lessons_extras.generate.save_lesson_draft",
        lambda lesson_markdown, _title, output_dir: _write_generated_file(
            output_dir, lesson_markdown
        ),
    )
    monkeypatch.setattr(
        "gptme_lessons_extras.utils.similarity.check_against_existing_lessons",
        lambda *_args, **_kwargs: [
            {
                "title": "Existing duplicate",
                "filepath": existing_lessons_dir / "patterns" / "existing.md",
                "similarity": 0.91,
            }
        ],
    )

    generated = generate_lessons_with_evolution(
        analysis_file=analysis_file,
        output_dir=output_dir,
        existing_lessons_dir=existing_lessons_dir,
        skip_duplicates=False,
        verbose=False,
    )

    assert len(generated) == 1
    assert generated[0].exists()
    assert generated[0].read_text() == "generated lesson markdown"
    captured = capsys.readouterr()
    assert "Similar lesson found (91% match): Existing duplicate" in captured.out
    assert "Location: patterns/existing.md" in captured.out
    assert "Generating anyway (--no-skip-duplicates)" in captured.out


def test_generate_lessons_with_evolution_warns_when_lessons_dir_missing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Missing lessons dir should warn even without verbose output."""
    analysis_file = tmp_path / "analysis.json"
    _write_analysis(analysis_file)
    missing_lessons_dir = tmp_path / "missing-lessons"
    output_dir = tmp_path / "output"

    monkeypatch.setattr(
        "gptme_lessons_extras.generate.gepa_lite_evolve",
        lambda **_kwargs: (
            "generated lesson markdown",
            {"scores": {"quality": 0.9, "specificity": 0.9}},
            [],
        ),
    )
    monkeypatch.setattr(
        "gptme_lessons_extras.generate.save_lesson_draft",
        lambda lesson_markdown, _title, output_dir: _write_generated_file(
            output_dir, lesson_markdown
        ),
    )

    generated = generate_lessons_with_evolution(
        analysis_file=analysis_file,
        output_dir=output_dir,
        existing_lessons_dir=missing_lessons_dir,
        verbose=False,
    )

    assert len(generated) == 1
    captured = capsys.readouterr()
    assert (
        f"Warning: Existing lessons directory not found: {missing_lessons_dir}"
        in captured.out
    )
    assert "Skipping duplicate check." in captured.out


def test_generate_lessons_with_evolution_handles_non_relative_match_location(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Duplicate reporting should not crash on out-of-tree match paths."""
    analysis_file = tmp_path / "analysis.json"
    _write_analysis(analysis_file)
    existing_lessons_dir = tmp_path / "lessons"
    existing_lessons_dir.mkdir()
    external_match = tmp_path / "other-root" / "existing.md"
    output_dir = tmp_path / "output"

    monkeypatch.setattr(
        "gptme_lessons_extras.generate.gepa_lite_evolve",
        lambda **_kwargs: (
            "generated lesson markdown",
            {"scores": {"quality": 0.9, "specificity": 0.9}},
            [],
        ),
    )
    monkeypatch.setattr(
        "gptme_lessons_extras.generate.save_lesson_draft",
        lambda lesson_markdown, _title, output_dir: _write_generated_file(
            output_dir, lesson_markdown
        ),
    )
    monkeypatch.setattr(
        "gptme_lessons_extras.utils.similarity.check_against_existing_lessons",
        lambda *_args, **_kwargs: [
            {
                "title": "External duplicate",
                "filepath": external_match,
                "similarity": 0.91,
            }
        ],
    )

    generated = generate_lessons_with_evolution(
        analysis_file=analysis_file,
        output_dir=output_dir,
        existing_lessons_dir=existing_lessons_dir,
        skip_duplicates=False,
        verbose=False,
    )

    assert len(generated) == 1
    captured = capsys.readouterr()
    assert f"Location: {external_match}" in captured.out


def _write_generated_file(output_dir: Path, lesson_markdown: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "generated.md"
    filepath.write_text(lesson_markdown)
    return filepath
