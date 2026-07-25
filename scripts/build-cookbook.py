#!/usr/bin/env python3
"""Build a static HTML index for the gptme interactive cookbook.

Usage:
    python3 scripts/build-cookbook.py [--cookbook-dir DIR] [--output FILE]

Output: cookbook/index.html (default)
"""

import argparse
import html
import re
import sys
from pathlib import Path

import yaml


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter and return (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    # Find closing --- at the start of a line; use a fixed-offset split so that
    # --- appearing inside YAML block scalars doesn't terminate early.
    m = re.search(r"\n---[ \t]*\n", text)
    if not m:
        return {}, text
    fm_text = text[3 : m.start()].strip()
    body = text[m.end() :].strip()
    try:
        meta = yaml.safe_load(fm_text) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    return meta, body


def extract_sections(body: str) -> dict[str, str]:
    """Extract ## section content as a dict."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


CATEGORY_LABELS = {
    "tool-use": "Tool Use",
    "multi-agent": "Multi-Agent",
    "context-management": "Context Management",
    "skill-composition": "Skill Composition",
    "custom-tools": "Custom Tools",
}

DIFFICULTY_COLORS = {
    "beginner": "#3fb950",
    "intermediate": "#d29922",
    "advanced": "#f85149",
}

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>gptme Interactive Cookbook</title>
  <style>
    :root {{
      --bg: #0d1117;
      --surface: #161b22;
      --border: #30363d;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #388bfd;
      --accent-hover: #58a6ff;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.6;
    }}
    header {{
      padding: 2rem 1.5rem 1rem;
      border-bottom: 1px solid var(--border);
      max-width: 900px;
      margin: 0 auto;
    }}
    header h1 {{ font-size: 1.75rem; margin-bottom: 0.5rem; }}
    header p {{ color: var(--muted); }}
    .filters {{
      max-width: 900px;
      margin: 1.5rem auto 0;
      padding: 0 1.5rem;
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
    }}
    .filter-btn {{
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--muted);
      padding: 0.3rem 0.75rem;
      border-radius: 1rem;
      cursor: pointer;
      font-size: 0.875rem;
      transition: border-color 0.15s, color 0.15s;
    }}
    .filter-btn:hover, .filter-btn.active {{
      border-color: var(--accent);
      color: var(--accent-hover);
    }}
    main {{
      max-width: 900px;
      margin: 1.5rem auto 3rem;
      padding: 0 1.5rem;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
      gap: 1rem;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      transition: border-color 0.15s;
    }}
    .card:hover {{ border-color: var(--accent); }}
    .card-header {{ display: flex; align-items: baseline; gap: 0.5rem; flex-wrap: wrap; }}
    .card h2 {{ font-size: 1rem; font-weight: 600; }}
    .badge {{
      font-size: 0.75rem;
      padding: 0.15rem 0.5rem;
      border-radius: 0.25rem;
      border: 1px solid;
      white-space: nowrap;
    }}
    .badge-category {{ border-color: var(--accent); color: var(--accent); background: #1c2d42; }}
    .badge-diff {{ border-color: transparent; }}
    .card p.desc {{ font-size: 0.9rem; color: var(--muted); }}
    .card .problem {{ font-size: 0.875rem; border-left: 3px solid var(--border); padding-left: 0.75rem; color: var(--muted); }}
    .card-footer {{ margin-top: auto; display: flex; gap: 0.5rem; flex-wrap: wrap; }}
    .btn {{
      display: inline-block;
      padding: 0.4rem 0.9rem;
      border-radius: 6px;
      font-size: 0.875rem;
      text-decoration: none;
      font-weight: 500;
      transition: opacity 0.15s;
    }}
    .btn:hover {{ opacity: 0.85; }}
    .btn-primary {{ background: var(--accent); color: #fff; }}
    .btn-secondary {{ background: transparent; border: 1px solid var(--border); color: var(--text); }}
    .tags {{ display: flex; gap: 0.35rem; flex-wrap: wrap; }}
    .tag {{
      font-size: 0.75rem;
      background: #21262d;
      border: 1px solid var(--border);
      padding: 0.1rem 0.45rem;
      border-radius: 0.25rem;
      color: var(--muted);
    }}
    footer {{
      text-align: center;
      padding: 1rem;
      color: var(--muted);
      font-size: 0.8rem;
      border-top: 1px solid var(--border);
    }}
    footer a {{ color: var(--accent); text-decoration: none; }}
  </style>
</head>
<body>
  <header>
    <h1>gptme Interactive Cookbook</h1>
    <p>Canonical patterns for building with gptme — browse, copy, and launch directly in your browser.</p>
  </header>
  <div class="filters" id="filters">
    <button class="filter-btn active" data-filter="all">All</button>
    {filter_buttons}
  </div>
  <main id="cards">
    {cards}
  </main>
  <footer>
    Built from <a href="https://github.com/gptme/gptme-contrib/tree/master/cookbook">gptme-contrib/cookbook</a> &mdash;
    <a href="https://gptme.org">gptme.org</a>
  </footer>
  <script>
    const btns = document.querySelectorAll('.filter-btn');
    const cards = document.querySelectorAll('.card');
    btns.forEach(btn => {{
      btn.addEventListener('click', () => {{
        btns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const filter = btn.dataset.filter;
        cards.forEach(card => {{
          card.style.display =
            filter === 'all' || card.dataset.category === filter ? '' : 'none';
        }});
      }});
    }});
  </script>
</body>
</html>
"""

CARD_TEMPLATE = """\
    <article class="card" data-category="{category}">
      <div class="card-header">
        <h2>{title}</h2>
        <span class="badge badge-category">{category_label}</span>
        <span class="badge badge-diff" style="color:{diff_color}; border-color:{diff_color}">{difficulty}</span>
      </div>
      <p class="desc">{description}</p>
      <div class="problem">{problem_snippet}</div>
      <div class="tags">{tags_html}</div>
      <div class="card-footer">
        <a href="{deep_link}" class="btn btn-primary" target="_blank" rel="noopener">Try in gptme.ai ↗</a>
        <a href="{source_file}" class="btn btn-secondary">View pattern</a>
      </div>
    </article>"""


def build_card(meta: dict, sections: dict, source_file: str) -> str:
    category = meta.get("category", "")
    difficulty = meta.get("difficulty", "beginner")
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]

    problem = sections.get("Problem", "")
    problem_snippet = (problem[:200] + "…") if len(problem) > 200 else problem
    # Collapse newlines for inline display
    problem_snippet = " ".join(problem_snippet.split())

    tags_html = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)

    # Sanitize deep_link: only allow http/https URLs
    raw_link = meta.get("deep_link", "https://gptme.ai/")
    deep_link = (
        raw_link
        if raw_link.startswith(("https://", "http://"))
        else "https://gptme.ai/"
    )

    return CARD_TEMPLATE.format(
        category=html.escape(category),
        category_label=html.escape(CATEGORY_LABELS.get(category, category)),
        title=html.escape(meta.get("title", "Untitled")),
        description=html.escape(meta.get("description", "")),
        difficulty=html.escape(difficulty),
        diff_color=html.escape(DIFFICULTY_COLORS.get(difficulty, "#8b949e")),
        problem_snippet=html.escape(problem_snippet),
        tags_html=tags_html,
        deep_link=html.escape(deep_link, quote=True),
        source_file=html.escape(source_file),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cookbook-dir", default="cookbook", help="Path to cookbook directory"
    )
    parser.add_argument(
        "--output", default="cookbook/index.html", help="Output HTML file"
    )
    args = parser.parse_args()

    cookbook_dir = Path(args.cookbook_dir)
    output_path = Path(args.output)

    pattern_files = sorted(
        f for f in cookbook_dir.glob("*.md") if f.name.lower() != "readme.md"
    )

    if not pattern_files:
        print(f"No pattern files found in {cookbook_dir}", file=sys.stderr)
        return 1

    cards_html = []
    categories: list[str] = []

    for path in pattern_files:
        text = path.read_text()
        meta, body = parse_frontmatter(text)
        sections = extract_sections(body)
        card = build_card(meta, sections, path.name)
        cards_html.append(card)
        cat = meta.get("category", "")
        if cat and cat not in categories:
            categories.append(cat)

    filter_buttons = "".join(
        f'<button class="filter-btn" data-filter="{html.escape(c, quote=True)}">'
        f"{html.escape(CATEGORY_LABELS.get(c, c))}</button>"
        for c in categories
    )

    page_html = HTML_TEMPLATE.format(
        filter_buttons=filter_buttons,
        cards="\n".join(cards_html),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(page_html)
    print(f"Built {len(pattern_files)} patterns → {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
