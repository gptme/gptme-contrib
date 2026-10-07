---
name: html-artifact-qa
description: "Use when a user asks a complex, multi-part, or deeply technical question that benefits from interactive HTML output: visualizations, collapsible sections, live code, diagrams. Do NOT use for simple factual questions, short code completions, or conversational replies."
license: MIT
compatibility: gptme (webui with artifact rendering)
metadata:
  author: gptme-contrib
  version: "0.1.0"
  tags:
    - ux
    - artifacts
    - html
    - qa
    - interactive
  requires_tools: []
  requires_skills: []
keywords:
  - "explain with html"
  - "interactive visualization"
  - "complex question html"
  - "show me how it works"
  - "visualize this"
  - "answer with interactive"
  - "how does X work with diagram"
  - "explain visually"
---

# HTML Artifact Q&A Skill

Respond to complex technical questions with rich, interactive HTML artifacts
rendered natively in gptme's webui.

## When to use

Fire this skill when the question has **two or more** of these complexity signals:

- **Multi-part**: asks "how/why/what" about a system with multiple components
- **Visual benefit**: the answer is clearer with a diagram, table, or chart
- **Technical depth**: involves algorithms, protocols, distributed systems, data
  structures, or debugging multi-layer stacks
- **Long question**: >3 sentences or multiple nested sub-questions

Simple lookups, one-liner code completions, and conversational replies do NOT
warrant an HTML artifact — prefer plain text for those.

## Rendering path

gptme renders `html` codeblocks as first-class artifacts via `artifacts_api.py`
(`RENDERABLE_KINDS` includes `"html"`). No extra tool calls needed: output an
HTML codeblock and the webui surfaces it as an iframe-previewed artifact.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Answer: &lt;img&gt; — example of an already-escaped topic</title>  <!-- user-derived text must already be escaped: see Output safety -->
  <style>
    /* minimal accessible CSS — dark-mode aware */
    body { font-family: system-ui, sans-serif; max-width: 820px; margin: 2rem auto; padding: 0 1rem; line-height: 1.6; color-scheme: light dark; }
    details summary { cursor: pointer; font-weight: 600; }
    pre { overflow-x: auto; background: #f4f4f4; padding: 1rem; border-radius: 4px; }
    @media (prefers-color-scheme: dark) { pre { background: #222; } }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ccc; padding: .5rem; text-align: left; }
  </style>
</head>
<body>
  <div id="qa-source" hidden>[user question — written HTML-escaped, e.g. &lt;img src=x&gt;]</div>
  <h1 id="qa-topic"></h1>
  <p>[1-2 sentence TL;DR — your own wording; if it quotes the user, escape it]</p>

  <details open>
    <summary>Section 1: [key concept]</summary>
    <p>...</p>
  </details>

  <details>
    <summary>Section 2: [next concept]</summary>
    <p>...</p>
  </details>

  <!-- Add diagrams as inline SVG, tables for comparisons, or <pre><code> for examples -->
  <script>
    // User-derived text lives ONLY in the hidden #qa-source text node, where it
    // was written HTML-escaped. Copy it into visible elements via textContent
    // (escaping is automatic in both directions). Never build a JS string
    // literal from user input, and never use innerHTML for it.
    document.getElementById('qa-topic').textContent =
      document.getElementById('qa-source').textContent;
  </script>
</body>
</html>
```

## Layout guidelines

- **TL;DR first**: a 1-2 sentence plain-language summary before the details.
- **`<details>` sections**: each major concept gets its own collapsible.
- **Comparisons → tables**: use `<table>` for side-by-side comparisons or option
  lists.
- **Code examples → `<pre><code>`**: syntax-highlighted if possible with a
  lightweight inline highlighter (prism.js from a CDN is fine).
- **Diagrams → inline SVG**: small ASCII-to-SVG or hand-crafted SVG for flow/
  state diagrams. No external chart library needed for simple diagrams.
- **Self-contained**: no external dependencies except optional CDN script tags.
  The artifact must render offline-first.

## Output safety: escape user-derived text

The question topic and any quoted user content are user-controlled strings.
**HTML-escape them before embedding** — a question like
`explain <img src=x onerror=alert(1)>` must render as text, not execute. Use one
of these two mechanisms, both shown in the template:

1. **Preferred — hidden source + `textContent`**: put the user-derived string
   (HTML-escaped) in a `hidden` source element, then copy it into visible
   elements via `textContent` (automatic escaping both ways; never
   `innerHTML`). **Never place user input inside a JavaScript string
   literal** — `</script>` in the input would break out of the script element.
2. **Static embedding — escape explicitly**: when you must write the text
   directly into the markup, run it through this routine first:
   `s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))`
   (`<` → `&lt;`, `>` → `&gt;`, `&` → `&amp;`, quotes → `&quot;`/`&#39;`).

Treat the artifact as untrusted input rendered in an iframe on the user's
machine: never interpolate raw user text into `<title>`, `<h1>`, attribute
values, or inline `<script>`.

## After the artifact

Follow the HTML codeblock with a one-paragraph plain-text summary for users
who skim or use the terminal interface. Do not repeat the full content — just
name the sections and where the key insight lives.

## Related

- gptme artifact infrastructure: `gptme/server/artifacts_api.py`
- `ArtifactDescriptor` (kind `iframe` / `live_app`): `gptme/message.py`
- Skill origin: `tasks/html-artifact-qa-skill.md` (idea #5780)
