# ruff: noqa: E402

import json
import sys
import types
from pathlib import Path

import pytest

# Only stub gptme when the real package is absent. Installing a bare
# types.ModuleType (which has no __path__) unconditionally pollutes sys.modules
# across multi-package test runs: the stub shadows the real package for all
# later tests in the same process, causing "gptme is not a package" errors.
try:
    import gptme as _real_gptme  # noqa: F401 — side-effect: populates sys.modules
except ImportError:
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


def _write_author_analysis(path: Path, title: str, context: str) -> None:
    analysis = {
        "conversation_id": "conv-123",
        "metadata": {
            "experiences": [
                {
                    "title": title,
                    "context": context,
                    "confidence": 0.95,
                }
            ]
        },
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


def _fake_lesson_markdown(moment, conversation_id: str) -> str:
    return "---\nstatus: active\n---\n# Fresh lesson\n"


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


def test_generate_lessons_from_analysis_skips_duplicate_check_when_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    analysis_file = tmp_path / "analysis.json"
    output_dir = tmp_path / "out"
    title = "Generate without duplicate checks"
    context = "Disabled duplicate checks should bypass similarity lookups."

    _write_author_analysis(analysis_file, title, context)

    similarity_called = False

    def fake_similarity(**kwargs):
        nonlocal similarity_called
        similarity_called = True
        return []

    monkeypatch.setattr(generate, "_check_existing_lesson_similarity", fake_similarity)
    monkeypatch.setattr(generate, "llm_author_reflect", _fake_lesson_markdown)
    monkeypatch.setattr(generate, "save_lesson_draft", _fake_save_lesson_draft)

    generated = generate.generate_lessons_from_analysis(
        analysis_file=analysis_file,
        output_dir=output_dir,
        check_existing=False,
    )

    assert similarity_called is False
    assert generated == [output_dir / "patterns" / "generated.md"]


def test_generate_lessons_from_analysis_uses_default_lessons_dir(
    monkeypatch, tmp_path: Path
) -> None:
    analysis_file = tmp_path / "analysis.json"
    output_dir = tmp_path / "out"
    title = "Use default lessons directory"
    context = "When no lessons dir is passed, the default lessons/ path should be used."
    lessons_dir = tmp_path / "lessons"

    _write_author_analysis(analysis_file, title, context)
    lessons_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    seen = {}

    def fake_similarity(**kwargs):
        seen["existing_lessons_dir"] = kwargs["existing_lessons_dir"]
        return []

    monkeypatch.setattr(generate, "_check_existing_lesson_similarity", fake_similarity)
    monkeypatch.setattr(generate, "llm_author_reflect", _fake_lesson_markdown)
    monkeypatch.setattr(generate, "save_lesson_draft", _fake_save_lesson_draft)

    generated = generate.generate_lessons_from_analysis(
        analysis_file=analysis_file,
        output_dir=output_dir,
        check_existing=True,
        existing_lessons_dir=None,
    )

    assert seen["existing_lessons_dir"].resolve() == lessons_dir.resolve()
    assert generated == [output_dir / "patterns" / "generated.md"]


def _raise_401(*args, **kwargs):
    raise RuntimeError("401 invalid x-api-key")


def test_generate_cli_exits_nonzero_when_every_generation_errors(
    monkeypatch, tmp_path: Path
) -> None:
    # #2362 E9: an LLM outage was reported as "No lessons generated", exit 0.
    from click.testing import CliRunner

    analysis_file = tmp_path / "analysis.json"
    _write_author_analysis(analysis_file, "Use patch tool", "ctx")
    monkeypatch.setattr(generate, "llm_author_reflect", _raise_401)

    result = CliRunner().invoke(
        generate.cli,
        [
            "generate",
            str(analysis_file),
            "-o",
            str(tmp_path / "out"),
            "--no-check-existing",
            "--existing-lessons-dir",
            str(tmp_path),
        ],
    )

    assert "Usage:" not in result.output
    assert result.exit_code == 1
    assert "401 invalid x-api-key" in result.output


def test_generate_lessons_with_evolution_raises_when_every_generation_errors(
    monkeypatch, tmp_path: Path
) -> None:
    analysis_file = tmp_path / "analysis.json"
    _write_analysis(analysis_file, "Use patch tool", "ctx")
    monkeypatch.setattr(generate, "gepa_lite_evolve", _raise_401)

    with pytest.raises(RuntimeError, match="all 1 lesson generation attempt"):
        generate.generate_lessons_with_evolution(
            analysis_file=analysis_file,
            output_dir=tmp_path / "out",
            check_existing=False,
        )


@pytest.mark.parametrize("evolution", [False, True])
@pytest.mark.parametrize("outcome", ["duplicate", "low_quality", "saved"])
def test_generation_error_with_completed_moment_is_not_total_failure(
    monkeypatch, tmp_path: Path, capsys, evolution: bool, outcome: str
) -> None:
    analysis_file = tmp_path / "analysis.json"
    moments = [
        {"title": title, "context": "ctx", "confidence": 0.95}
        for title in ["Completed moment", "Failed moment"]
    ]
    analysis_file.write_text(
        json.dumps(
            {
                "conversation_id": "conv-123",
                "experiences": moments,
                "metadata": {"experiences": moments},
            }
        ),
        encoding="utf-8",
    )
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()

    def similarity(**kwargs):
        if outcome == "duplicate" and kwargs["title"] == "Completed moment":
            return [
                {
                    "title": "Existing lesson",
                    "similarity": 1.0,
                    "filepath": lessons_dir / "existing.md",
                }
            ]
        return []

    def reflect(moment, conversation_id):
        if moment["title"] == "Failed moment":
            raise RuntimeError("401 invalid x-api-key")
        return _fake_lesson_markdown(moment, conversation_id)

    def evolve(**kwargs):
        return (
            reflect(kwargs["moment"], kwargs["conversation_id"]),
            {"scores": {"quality": 0.1}},
            [],
        )

    monkeypatch.setattr(generate, "_check_existing_lesson_similarity", similarity)
    monkeypatch.setattr(generate, "llm_author_reflect", reflect)
    monkeypatch.setattr(generate, "gepa_lite_evolve", evolve)
    monkeypatch.setattr(
        generate, "llm_judge_score", lambda *args: {"scores": {"quality": 0.1}}
    )
    monkeypatch.setattr(generate, "save_lesson_draft", _fake_save_lesson_draft)
    function = (
        generate.generate_lessons_with_evolution
        if evolution
        else generate.generate_lessons_from_analysis
    )

    result = function(
        analysis_file,
        tmp_path / "out",
        existing_lessons_dir=lessons_dir,
        judge_threshold=0.75 if outcome == "low_quality" else None,
    )

    assert result == (
        [tmp_path / "out" / "patterns" / "generated.md"] if outcome == "saved" else []
    )
    if not evolution and outcome == "low_quality":
        assert "Skipped 1 lessons due to low quality scores" in capsys.readouterr().out


@pytest.mark.parametrize("evolution", [False, True])
def test_total_generation_failure_preserves_original_cause(
    monkeypatch, tmp_path: Path, evolution: bool
) -> None:
    analysis_file = tmp_path / "analysis.json"
    writer = _write_analysis if evolution else _write_author_analysis
    writer(analysis_file, "Use patch tool", "ctx")
    original = KeyError("scores")

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(
        generate, "gepa_lite_evolve" if evolution else "llm_author_reflect", fail
    )
    function = (
        generate.generate_lessons_with_evolution
        if evolution
        else generate.generate_lessons_from_analysis
    )
    with pytest.raises(RuntimeError, match="all 1 lesson generation attempt") as exc:
        function(analysis_file, tmp_path / "out", check_existing=False)

    assert exc.value.__cause__ is original
