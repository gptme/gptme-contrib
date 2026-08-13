from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jsonschema  # type: ignore
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate-presentation-html.py"
SCHEMA = REPO_ROOT / "knowledge" / "json-schemas" / "presentation.schema.json"
DEMO = REPO_ROOT / "knowledge" / "decks" / "agentic-presentation-demo.json"

spec = importlib.util.spec_from_file_location("generate_presentation_html", SCRIPT)
assert spec is not None
assert spec.loader is not None
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def _sample_deck() -> dict:
    return {
        "title": "Example",
        "metadata": {
            "author": "Bob",
            "date": "2026-08-13",
            "theme": {"accent": "#0F766E", "secondary": "#C2410C"},
        },
        "slides": [
            {
                "id": "intro",
                "type": "title-slide",
                "title": "Example",
                "subtitle": "A tiny deck",
                "animations": [{"type": "fade-in", "duration": 300}],
            },
            {
                "id": "content",
                "type": "content",
                "title": "What matters",
                "body": "Structured input, deterministic output.",
                "bullets": ["Validates first", "Renders offline"],
                "animations": [{"type": "slide-in", "target": "body"}],
            },
            {
                "id": "code",
                "type": "code-reveal",
                "title": "Code reveal",
                "language": "python",
                "code": "print('hello')\nprint('world')",
                "animations": [{"type": "line-reveal", "target": "code"}],
            },
        ],
    }


def test_schema_accepts_sample_and_demo_decks():
    schema = json.loads(SCHEMA.read_text())

    jsonschema.validate(_sample_deck(), schema)
    jsonschema.validate(json.loads(DEMO.read_text()), schema)


def test_schema_rejects_code_reveal_without_code():
    schema = json.loads(SCHEMA.read_text())
    deck = _sample_deck()
    del deck["slides"][2]["code"]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(deck, schema)


def test_render_html_is_self_contained_and_escapes_content():
    deck = _sample_deck()
    deck["slides"][1]["body"] = "<script>alert(1)</script>"

    html = generator.render_html(deck)

    assert "<!DOCTYPE html>" in html
    assert "stylesheet" not in html
    assert "https://cdn" not in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "function showSlide" in html


def test_render_html_includes_three_mvp_animation_primitives():
    html = generator.render_html(_sample_deck())

    assert "animate-fade-in" in html
    assert "animate-slide-in" in html
    assert "animate-line-reveal" in html
    assert "requestAnimationFrame" in html
    assert "data-animations=" in html


def test_generator_writes_single_file_under_500kb(tmp_path: Path):
    input_path = tmp_path / "deck.json"
    output_path = tmp_path / "deck.html"
    input_path.write_text(json.dumps(_sample_deck()), encoding="utf-8")

    assert (
        generator.main([str(input_path), "-o", str(output_path), "--max-kb", "500"])
        == 0
    )

    output = output_path.read_text(encoding="utf-8")
    assert output_path.stat().st_size < 500 * 1024
    assert "<style>" in output
    assert "<script>" in output
    assert output.count('<section class="slide') == 3


def test_generator_keeps_twenty_slide_deck_under_500kb(tmp_path: Path):
    deck = _sample_deck()
    content_slide = deck["slides"][1]
    deck["slides"] = [
        {
            **content_slide,
            "id": f"slide-{index:02d}",
            "title": f"Slide {index:02d}",
        }
        for index in range(20)
    ]
    input_path = tmp_path / "twenty.json"
    output_path = tmp_path / "twenty.html"
    input_path.write_text(json.dumps(deck), encoding="utf-8")

    assert (
        generator.main([str(input_path), "-o", str(output_path), "--max-kb", "500"])
        == 0
    )

    assert output_path.stat().st_size < 500 * 1024


def test_validate_presentation_rejects_duplicate_slide_ids():
    deck = _sample_deck()
    deck["slides"][1]["id"] = "intro"

    with pytest.raises(ValueError, match="duplicate slide id"):
        generator.validate_presentation(deck)
