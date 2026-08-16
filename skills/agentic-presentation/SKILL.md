---
name: agentic-presentation
description: Generate an offline, single-file HTML presentation from structured slide JSON. Use when turning notes, research, demos, or technical explanations into an agent-generated slide deck.
license: MIT
compatibility: "Requires Python 3.10+ (stdlib only for rendering)"
metadata:
  author: bob
  version: "0.1.0"
  tags: [presentations, content, html, slides, agents]
  requires_tools: [shell]
  requires_skills: []
---

# Agentic Presentation

Turn structured slide JSON into a standalone HTML deck that opens in any browser, offline.

## When to Use

**Use for**: notes, research summaries, demos, or technical explanations that should be delivered as slides.

**Do not use for**: full web apps, long-form blog posts, or hosted dashboards.

## Bundled Files

| File | Purpose |
|------|---------|
| `generate_presentation_html.py` | Renderer: validates deck JSON, emits a single self-contained HTML file |
| `present.py` | Convenience wrapper: render and optionally open in a browser |
| `presentation.schema.json` | JSON Schema (draft-07) for the deck format |
| `examples/agentic-presentation-demo.json` | Working demo deck |
| `tests/test_generate_presentation_html.py` | Tests for the renderer and schema |

## Workflow

1. Convert source notes into slide-deck JSON that conforms to `presentation.schema.json`.
2. Keep slide count tight: one idea per slide, short text, code blocks only when they teach the point.
3. Use these MVP slide types: `title-slide`, `content`, `code-reveal`, `code-and-text`, `image`, `gallery`.
4. Use these MVP animations: `fade-in`, `slide-in`, `line-reveal`. `zoom` and `spin` are schema-reserved but should stay rare.
5. Render locally — three ways (paths shown from the repo root):

```bash
# Quick path: render + open in browser
python3 skills/agentic-presentation/present.py deck.json --live

# Explicit path: render to a named file
python3 skills/agentic-presentation/present.py deck.json -o /tmp/slides.html

# Low-level: call the generator directly
python3 skills/agentic-presentation/generate_presentation_html.py \
  skills/agentic-presentation/examples/agentic-presentation-demo.json \
  -o /tmp/agentic-presentation-demo.html
```

The rendered HTML has built-in keyboard navigation: **→** or **Space** / **Enter** to advance, **←** to go back.

## Output Contract

- The generated file must be standalone HTML with inline CSS and JavaScript.
- Do not depend on CDNs for the MVP path; offline rendering is a hard constraint.
- Keep generated HTML below 500KB for ordinary decks. Use `--max-kb` to enforce the limit.
- Escape all user-provided text through the generator; do not hand-write HTML fragments inside slide JSON.

## Authoring Guidance

Good slide JSON is compact and structured:

```json
{
  "id": "code",
  "type": "code-reveal",
  "title": "Generator shape",
  "language": "python",
  "code": "deck = load_presentation(path)\nhtml = render_html(deck)",
  "animations": [{"type": "line-reveal", "target": "code"}]
}
```

Use `content` slides for prose and bullets. Use `code-and-text` only when the prose and code need to be compared side by side. Use image slides only when the image is the point, not decoration.

## Verification

Rendering needs only the Python standard library. The tests additionally need `pytest` and `jsonschema`.

```bash
# Run the focused tests after changing the generator or schema
uv run --with pytest --with jsonschema pytest skills/agentic-presentation/tests/ -q
```

For visual verification, render the demo and open the HTML file in a browser. Check keyboard navigation, the slide counter, and the three MVP animations.

## Related

- [Skills README](../README.md) - Skills system overview
