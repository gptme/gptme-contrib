from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = SKILL_ROOT / "presentation.schema.json"
DEMO = SKILL_ROOT / "examples" / "agentic-presentation-demo.json"

sys.path.insert(0, str(SKILL_ROOT))

import generate_presentation_html as generator  # noqa: E402


def _sample_deck() -> dict:
    return {
        "title": "Example",
        "metadata": {
            "author": "Example Author",
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
    # Rendering needs only the stdlib; the schema tests need jsonschema.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())

    jsonschema.validate(_sample_deck(), schema)
    jsonschema.validate(json.loads(DEMO.read_text()), schema)


def test_schema_rejects_code_reveal_without_code():
    jsonschema = pytest.importorskip("jsonschema")
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


def test_animation_target_is_not_interpolated_into_javascript():
    deck = _sample_deck()
    deck["slides"][1]["animations"][0]["target"] = 'body"];alert(1)//'

    html = generator.render_html(deck)

    assert 'slide.querySelector(`[data-role="${target}"]`)' not in html
    assert "CSS.escape(target)" in html


@pytest.mark.parametrize(
    ("slide_type", "field", "value", "message"),
    [
        ("image", "image", "not-an-object", "image must be an object"),
        ("gallery", "images", "not-an-array", "images must be an array"),
        ("gallery", "images", ["not-an-object"], "image 1 must be an object"),
    ],
)
def test_validate_presentation_rejects_malformed_images(
    slide_type: str, field: str, value: object, message: str
):
    deck = _sample_deck()
    deck["slides"][1] = {
        "id": "media",
        "type": slide_type,
        "title": "Media",
        field: value,
    }

    with pytest.raises(ValueError, match=message):
        generator.validate_presentation(deck)


def test_validate_presentation_rejects_duplicate_slide_ids():
    deck = _sample_deck()
    deck["slides"][1]["id"] = "intro"

    with pytest.raises(ValueError, match="duplicate slide id"):
        generator.validate_presentation(deck)


def test_validate_presentation_rejects_non_object_deck():
    with pytest.raises(ValueError, match="presentation must be an object"):
        generator.validate_presentation([])


def test_validate_presentation_rejects_unsafe_theme_color():
    deck = _sample_deck()
    deck["metadata"]["theme"]["accent"] = "red; } body { display: none"

    with pytest.raises(ValueError, match="theme accent must be a six-digit hex color"):
        generator.validate_presentation(deck)


def test_content_body_and_bullets_have_distinct_animation_targets():
    html = generator.render_html(_sample_deck())

    assert html.count('data-role="body"') == 1
    assert 'data-role="bullets"' in html


def test_runtime_animation_types_match_schema():
    schema = json.loads(SCHEMA.read_text())
    schema_types = set(schema["definitions"]["animation"]["properties"]["type"]["enum"])

    assert generator.SUPPORTED_ANIMATION_TYPES == schema_types


def test_animation_timing_preserves_zero_values():
    javascript = generator._javascript()

    assert "animation.duration ?? 450" in javascript
    assert "animation.delay ?? 0" in javascript
    assert "animation.stagger ?? 80" in javascript


def test_keyboard_navigation_prevents_button_default_click():
    javascript = generator._javascript()

    assert javascript.count("event.preventDefault();") == 2


def test_live_wrapper_defaults_to_durable_index_html():
    source = (SKILL_ROOT / "present.py").read_text()

    assert 'output = args.output or Path("index.html")' in source
    assert "mkdtemp" not in source


def test_empty_animations_list_produces_no_animation():
    deck = _sample_deck()
    deck["slides"][0]["animations"] = []

    html = generator.render_html(deck)

    payload_start = html.index("data-animations='") + len("data-animations='")
    payload_end = html.index("'", payload_start)

    raw = html[payload_start:payload_end]
    # The attribute value is HTML-escaped; unescape to get the JSON
    import html as html_module

    animations = json.loads(html_module.unescape(raw))
    assert animations == [], f"expected empty animations, got {animations!r}"


def test_validate_presentation_rejects_invalid_slide_id():
    deck = _sample_deck()
    deck["slides"][0]["id"] = "bad id!"  # space and ! not in pattern

    with pytest.raises(ValueError, match="must match"):
        generator.validate_presentation(deck)


def test_validate_presentation_accepts_valid_slide_id_patterns():
    for valid_id in ("a", "slide-01", "MySlide_2", "A1"):
        deck = _sample_deck()
        deck["slides"][0]["id"] = valid_id
        generator.validate_presentation(deck)  # must not raise
