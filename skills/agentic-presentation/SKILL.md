---
name: agentic-presentation
description: Generate an offline, single-file HTML presentation from structured slide JSON.
when_to_use: "Use when turning notes, research, demos, or technical explanations into an agent-generated slide deck. Do not use for full web apps, long-form blog posts, or hosted dashboards."
license: MIT
compatibility: "Bob workspace; Python 3.10+"
metadata:
  author: bob
  version: "0.1.0"
  tags:
    - presentations
    - content
    - html
    - agents
  requires_tools:
    - shell
  requires_skills: []
keywords:
  - agentic presentation
  - generate presentation html
  - presentation schema
  - slide deck json
  - offline html deck
---

# Agentic Presentation

## Workflow

1. Convert source notes into `knowledge/json-schemas/presentation.schema.json`.
2. Keep slide count tight: one idea per slide, short text, code blocks only when they teach the point.
3. Use these MVP slide types: `title-slide`, `content`, `code-reveal`, `code-and-text`, `image`, `gallery`.
4. Use these MVP animations: `fade-in`, `slide-in`, `line-reveal`. `zoom` and `spin` are schema-reserved but should stay rare.
5. Render locally:

```bash
python3 scripts/generate-presentation-html.py knowledge/decks/agentic-presentation-demo.json -o /tmp/agentic-presentation-demo.html
```

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

Run the focused tests after changing the generator or schema:

```bash
uv run pytest tests/test_generate_presentation_html.py -q
```

For visual verification, render the demo and open the HTML file in a browser. Check keyboard navigation, the slide counter, and the three MVP animations.
