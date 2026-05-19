# ruff: noqa: E402

import json
import sys
import types
from pathlib import Path

fake_gptme = types.ModuleType("gptme")
fake_llm = types.ModuleType("gptme.llm")
fake_message = types.ModuleType("gptme.message")


def _unused_reply(*args, **kwargs):
    raise NotImplementedError


class _FakeMessage:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


fake_llm.reply = _unused_reply
fake_message.Message = _FakeMessage
fake_gptme.llm = fake_llm
fake_gptme.message = fake_message

sys.modules.setdefault("gptme", fake_gptme)
sys.modules.setdefault("gptme.llm", fake_llm)
sys.modules.setdefault("gptme.message", fake_message)

from gptme_lessons_extras import generate


def _write_analysis(path: Path, title: str, context: str) -> None:
    analysis = {
        "conversation_id": "conv-123",
        "experiences": [
            {
                "title": title,
                "context": context,
                "confidence": 0.95,
            }
        ],
    }
    path.write_text(json.dumps(analysis), encoding="utf-8")


def _write_existing_lesson(path: Path, title: str, context: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
status: active
---
# {title}

## Context
{context}
""",
        encoding="utf-8",
    )


def _fake_save_lesson_draft(lesson_markdown: str, title: str, output_dir: Path) -> Path:
    path = output_dir / "patterns" / "generated.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lesson_markdown, encoding="utf-8")
    return path


def test_generate_lessons_with_evolution_skips_similar_existing_lessons(
    monkeypatch, tmp_path: Path
) -> None:
    analysis_file = tmp_path / "analysis.json"
    lessons_dir = tmp_path / "lessons"
    output_dir = tmp_path / "out"
    title = "Avoid duplicate lessons"
    context = "When the same lesson already exists, skip regeneration."

    _write_analysis(analysis_file, title, context)
    _write_existing_lesson(lessons_dir / "patterns" / "existing.md", title, context)

    called = False

    def fake_gepa_lite_evolve(**kwargs):
        nonlocal called
        called = True
        return "", {"scores": {"quality": 1.0}}, []

    monkeypatch.setattr(generate, "gepa_lite_evolve", fake_gepa_lite_evolve)

    generated = generate.generate_lessons_with_evolution(
        analysis_file=analysis_file,
        output_dir=output_dir,
        check_existing=True,
        existing_lessons_dir=lessons_dir,
        skip_duplicates=True,
    )

    assert generated == []
    assert called is False


def test_generate_lessons_with_evolution_generates_when_duplicates_allowed(
    monkeypatch, tmp_path: Path
) -> None:
    analysis_file = tmp_path / "analysis.json"
    lessons_dir = tmp_path / "lessons"
    output_dir = tmp_path / "out"
    title = "Avoid duplicate lessons"
    context = "When the same lesson already exists, skip regeneration."

    _write_analysis(analysis_file, title, context)
    _write_existing_lesson(lessons_dir / "patterns" / "existing.md", title, context)

    calls = {"count": 0}

    def fake_gepa_lite_evolve(**kwargs):
        calls["count"] += 1
        return (
            "---\nstatus: active\n---\n# Fresh lesson\n",
            {"scores": {"quality": 1.0}},
            [],
        )

    monkeypatch.setattr(generate, "gepa_lite_evolve", fake_gepa_lite_evolve)
    monkeypatch.setattr(generate, "save_lesson_draft", _fake_save_lesson_draft)

    generated = generate.generate_lessons_with_evolution(
        analysis_file=analysis_file,
        output_dir=output_dir,
        check_existing=True,
        existing_lessons_dir=lessons_dir,
        skip_duplicates=False,
    )

    assert calls["count"] == 1
    assert generated == [output_dir / "patterns" / "generated.md"]


def test_generate_lessons_with_evolution_ignores_missing_existing_dir(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    analysis_file = tmp_path / "analysis.json"
    missing_lessons_dir = tmp_path / "missing-lessons"
    output_dir = tmp_path / "out"

    _write_analysis(
        analysis_file,
        "Generate without existing lessons directory",
        "Missing lesson directories should disable duplicate checks cleanly.",
    )

    calls = {"count": 0}

    def fake_gepa_lite_evolve(**kwargs):
        calls["count"] += 1
        return (
            "---\nstatus: active\n---\n# Fresh lesson\n",
            {"scores": {"quality": 1.0}},
            [],
        )

    monkeypatch.setattr(generate, "gepa_lite_evolve", fake_gepa_lite_evolve)
    monkeypatch.setattr(generate, "save_lesson_draft", _fake_save_lesson_draft)

    generated = generate.generate_lessons_with_evolution(
        analysis_file=analysis_file,
        output_dir=output_dir,
        check_existing=True,
        existing_lessons_dir=missing_lessons_dir,
        skip_duplicates=True,
    )

    captured = capsys.readouterr()

    assert "Skipping duplicate check." in captured.out
    assert calls["count"] == 1
    assert generated == [output_dir / "patterns" / "generated.md"]
