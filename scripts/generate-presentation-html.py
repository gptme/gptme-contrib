#!/usr/bin/env python3
"""Generate a self-contained HTML slide deck from presentation JSON."""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any, cast

_CSS_COLOR_RE = re.compile(
    r"^("
    r"#[0-9a-fA-F]{3,8}"  # hex: #RGB #RRGGBB #RRGGBBAA
    r"|rgba?\(\s*\d+%?\s*,\s*\d+%?\s*,\s*\d+%?(\s*,\s*[\d.]+)?\s*\)"  # rgb/rgba
    r"|hsl\(\s*\d+\s*,\s*\d+%\s*,\s*\d+%\s*\)"  # hsl
    r"|[a-zA-Z]{2,30}"  # named colors (white, black, transparent, etc.)
    r")$"
)

SUPPORTED_SLIDE_TYPES = {
    "title-slide",
    "content",
    "code-reveal",
    "code-and-text",
    "image",
    "gallery",
}
SUPPORTED_ANIMATION_TYPES = {
    "fade-in",
    "slide-in",
    "line-reveal",
    "line-by-line-reveal",
    "zoom",
    "spin",
}
DEFAULT_THEME = {
    "background": "#111418",
    "surface": "#F7F4EF",
    "foreground": "#14171A",
    "muted": "#5E6872",
    "accent": "#0F766E",
    "secondary": "#C2410C",
}


def load_presentation(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    validate_presentation(data)
    return cast(dict[str, Any], data)


def validate_presentation(deck: dict[str, Any]) -> None:
    if not isinstance(deck.get("title"), str) or not deck["title"].strip():
        raise ValueError("presentation must include a non-empty title")

    slides = deck.get("slides")
    if not isinstance(slides, list) or not slides:
        raise ValueError("presentation must include at least one slide")

    seen_ids: set[str] = set()
    for index, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            raise ValueError(f"slide {index} must be an object")
        slide_id = slide.get("id")
        if not isinstance(slide_id, str) or not slide_id:
            raise ValueError(f"slide {index} must include id")
        if slide_id in seen_ids:
            raise ValueError(f"duplicate slide id: {slide_id}")
        seen_ids.add(slide_id)

        slide_type = slide.get("type")
        if slide_type not in SUPPORTED_SLIDE_TYPES:
            raise ValueError(f"unsupported slide type for {slide_id}: {slide_type}")
        if not isinstance(slide.get("title"), str) or not slide["title"].strip():
            raise ValueError(f"slide {slide_id} must include title")
        if slide_type == "code-reveal" and "code" not in slide:
            raise ValueError(f"slide {slide_id} of type code-reveal requires code")
        if slide_type == "code-and-text" and (
            "code" not in slide or "body" not in slide
        ):
            raise ValueError(
                f"slide {slide_id} of type code-and-text requires body and code"
            )
        if slide_type == "image" and "image" not in slide:
            raise ValueError(f"slide {slide_id} of type image requires image")
        if slide_type == "image" and "image" in slide:
            img = slide["image"]
            if not isinstance(img, dict) or "src" not in img:
                raise ValueError(
                    f"slide {slide_id} image must be an object with a 'src' field"
                )
            if not isinstance(img["src"], str) or not img["src"]:
                raise ValueError(
                    f"slide {slide_id} image 'src' must be a non-empty string"
                )
        if slide_type == "gallery" and "images" not in slide:
            raise ValueError(f"slide {slide_id} of type gallery requires images")
        if slide_type == "gallery":
            for img_index, img in enumerate(slide.get("images", []), start=1):
                if not isinstance(img, dict) or "src" not in img:
                    raise ValueError(
                        f"slide {slide_id} image {img_index} must be an object with a 'src' field"
                    )
                if not isinstance(img["src"], str) or not img["src"]:
                    raise ValueError(
                        f"slide {slide_id} image {img_index} 'src' must be a non-empty string"
                    )

        for animation in slide.get("animations", []):
            animation_type = (
                animation.get("type") if isinstance(animation, dict) else None
            )
            if animation_type not in SUPPORTED_ANIMATION_TYPES:
                raise ValueError(
                    f"slide {slide_id} has unsupported animation: {animation_type}"
                )
            if isinstance(animation, dict) and "target" in animation:
                target = animation["target"]
                if not isinstance(target, str) or not re.fullmatch(
                    r"[a-z][a-z0-9-]*", target
                ):
                    raise ValueError(
                        f"slide {slide_id} animation target {target!r} must contain "
                        "only lowercase letters, digits, and hyphens"
                    )
            if isinstance(animation, dict):
                for field in ("duration", "delay", "stagger"):
                    value = animation.get(field)
                    if value is not None and (not isinstance(value, int) or value < 0):
                        raise ValueError(
                            f"slide {slide_id} animation '{field}' must be a "
                            f"non-negative integer, got {value!r}"
                        )

        # Type-check slide fields so hand-written or schema-bypassed decks fail
        # early with a clear error rather than crashing inside a render function.
        if slide_type in ("content", "code-and-text"):
            bullets = slide.get("bullets")
            if bullets is not None and not isinstance(bullets, list):
                raise ValueError(
                    f"slide {slide_id} 'bullets' must be a list, "
                    f"got {type(bullets).__name__}"
                )
            body = slide.get("body")
            if body is not None and not isinstance(body, str):
                raise ValueError(
                    f"slide {slide_id} 'body' must be a string, "
                    f"got {type(body).__name__}"
                )
        if slide_type in ("code-reveal", "code-and-text"):
            code = slide.get("code")
            if code is not None and not isinstance(code, str):
                raise ValueError(
                    f"slide {slide_id} 'code' must be a string, "
                    f"got {type(code).__name__}"
                )
            language = slide.get("language")
            if language is not None and not isinstance(language, str):
                raise ValueError(
                    f"slide {slide_id} 'language' must be a string, "
                    f"got {type(language).__name__}"
                )


def render_html(deck: dict[str, Any]) -> str:
    theme = _theme(deck)
    title = _escape(deck["title"])
    metadata = deck.get("metadata", {})
    author = _escape(metadata.get("author", ""))
    date = _escape(metadata.get("date", ""))
    slides = deck["slides"]
    rendered_slides = "\n".join(
        _render_slide(slide, index, len(slides)) for index, slide in enumerate(slides)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
{_css(theme)}
  </style>
</head>
<body>
  <main class="deck" data-slide-count="{len(slides)}">
{rendered_slides}
  </main>
  <nav class="controls" aria-label="Presentation controls">
    <button type="button" class="icon-button" id="prev" aria-label="Previous slide">‹</button>
    <output id="counter" aria-live="polite">1 / {len(slides)}</output>
    <button type="button" class="icon-button" id="next" aria-label="Next slide">›</button>
  </nav>
  <footer class="meta">{author}{" · " if author and date else ""}{date}</footer>
  <script>
{_javascript()}
  </script>
</body>
</html>
"""


def _theme(deck: dict[str, Any]) -> dict[str, str]:
    metadata = deck.get("metadata", {})
    custom = metadata.get("theme", {}) if isinstance(metadata, dict) else {}
    merged = {}
    for k, v in custom.items():
        if k not in DEFAULT_THEME:
            continue
        if not isinstance(v, str) or not _CSS_COLOR_RE.match(v.strip()):
            raise ValueError(
                f"theme.{k} contains an unsafe CSS value: {v!r}. "
                "Use a hex colour (#rrggbb), rgb/rgba(), hsl(), or a named colour."
            )
        merged[k] = v.strip()
    return {**DEFAULT_THEME, **merged}


def _render_slide(slide: dict[str, Any], index: int, total: int) -> str:
    slide_type = slide["type"]
    title = _escape(slide["title"])
    subtitle = _escape(slide.get("subtitle", ""))
    active_class = " active" if index == 0 else ""
    animations = _animation_payload(slide)
    content = _render_slide_content(slide)

    return f"""    <section class="slide slide-{slide_type}{active_class}" id="{_attr(slide["id"])}" data-index="{index}" data-animations='{animations}' aria-label="Slide {index + 1} of {total}">
      <div class="slide-inner">
        <p class="kicker">{index + 1:02d} / {total:02d}</p>
        <h1 data-role="title">{title}</h1>
        {f'<p class="subtitle" data-role="subtitle">{subtitle}</p>' if subtitle else ""}
        {content}
      </div>
    </section>"""


def _render_slide_content(slide: dict[str, Any]) -> str:
    slide_type = slide["type"]
    if slide_type == "title-slide":
        return ""
    if slide_type == "content":
        return _render_body_and_bullets(slide)
    if slide_type == "code-reveal":
        return _render_code(slide)
    if slide_type == "code-and-text":
        return f"""<div class="split">
          <div>{_render_body_and_bullets(slide)}</div>
          {_render_code(slide)}
        </div>"""
    if slide_type == "image":
        return _render_image(slide["image"])
    if slide_type == "gallery":
        return _render_gallery(slide.get("images", []))
    raise ValueError(f"unsupported slide type: {slide_type}")


def _render_body_and_bullets(slide: dict[str, Any]) -> str:
    body = _escape(slide.get("body", ""))
    bullets = slide.get("bullets", [])
    bullet_html = ""
    if bullets:
        items = "\n".join(
            f'            <li class="reveal-line">{_escape(item)}</li>'
            for item in bullets
        )
        bullet_html = f"""          <ul class="bullets">
{items}
          </ul>"""
    body_html = f'<p class="body">{body}</p>' if body else ""
    # Wrap in a single container so data-role="body" is unambiguous — if both
    # <p> and <ul> carried the role, querySelector would return only the first
    # element and animations targeting "body" would silently skip the list.
    return f"""<div class="body-container" data-role="body">
{body_html}
{bullet_html}
        </div>"""


def _render_code(slide: dict[str, Any]) -> str:
    language = _escape(slide.get("language", "text"))
    lines = str(slide.get("code", "")).splitlines() or [""]
    rendered = "\n".join(
        f'<span class="code-line reveal-line" style="--line-index: {index}">{_escape(line)}</span>'
        for index, line in enumerate(lines)
    )
    return f"""<figure class="code-panel" data-role="code">
          <figcaption>{language}</figcaption>
          <pre><code>{rendered}</code></pre>
        </figure>"""


def _render_image(image: dict[str, Any]) -> str:
    caption = _escape(image.get("caption", ""))
    return f"""<figure class="image-panel" data-role="image">
          <img src="{_attr(image["src"])}" alt="{_attr(image.get("alt", ""))}">
          {f"<figcaption>{caption}</figcaption>" if caption else ""}
        </figure>"""


def _render_gallery(images: list[dict[str, Any]]) -> str:
    rendered = "\n".join(_render_image(image) for image in images)
    return f'<div class="gallery" data-role="gallery">\n{rendered}\n        </div>'


def _animation_payload(slide: dict[str, Any]) -> str:
    # Use explicit key check so `"animations": []` (author intent: no animation)
    # is preserved as an empty list instead of being replaced by the default.
    animations = (
        slide["animations"]
        if "animations" in slide
        else [{"type": "fade-in", "duration": 350}]
    )
    normalized = []
    for animation in animations:
        item = dict(animation)
        if item.get("type") == "line-by-line-reveal":
            item["type"] = "line-reveal"
        normalized.append(item)
    return _attr(json.dumps(normalized, separators=(",", ":")))


def _css(theme: dict[str, str]) -> str:
    return f"""    :root {{
      --background: {theme["background"]};
      --surface: {theme["surface"]};
      --foreground: {theme["foreground"]};
      --muted: {theme["muted"]};
      --accent: {theme["accent"]};
      --secondary: {theme["secondary"]};
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      overflow: hidden;
      background: var(--background);
      color: var(--foreground);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    .deck {{ min-height: 100vh; position: relative; }}
    .slide {{
      display: none;
      min-height: 100vh;
      padding: clamp(1.5rem, 4vw, 4rem);
      place-items: center;
    }}
    .slide.active {{ display: grid; }}
    .slide-inner {{
      width: min(1080px, 100%);
      min-height: min(680px, calc(100vh - 9rem));
      display: grid;
      align-content: center;
      gap: 1.25rem;
      padding: clamp(1.25rem, 4vw, 3rem);
      background: var(--surface);
      border: 1px solid color-mix(in srgb, var(--foreground) 14%, transparent);
      border-radius: 8px;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.28);
    }}
    .kicker {{
      margin: 0;
      color: var(--accent);
      font-size: 0.8rem;
      font-weight: 700;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0;
      max-width: 14ch;
      font-size: clamp(2.8rem, 8vw, 5.8rem);
      line-height: 0.98;
      letter-spacing: 0;
    }}
    .slide-content h1, .slide-code-reveal h1, .slide-code-and-text h1,
    .slide-image h1, .slide-gallery h1 {{
      font-size: clamp(2rem, 5vw, 4rem);
      max-width: 18ch;
    }}
    .subtitle, .body {{
      max-width: 70ch;
      margin: 0;
      color: var(--muted);
      font-size: clamp(1.05rem, 2.4vw, 1.55rem);
      line-height: 1.5;
    }}
    .bullets {{
      display: grid;
      gap: 0.7rem;
      margin: 0;
      padding-left: 1.4rem;
      color: var(--foreground);
      font-size: clamp(1rem, 2vw, 1.35rem);
      line-height: 1.45;
    }}
    .split {{
      display: grid;
      grid-template-columns: minmax(0, 0.9fr) minmax(280px, 1.1fr);
      gap: clamp(1rem, 4vw, 2.5rem);
      align-items: start;
    }}
    .code-panel {{
      margin: 0;
      width: 100%;
      overflow: hidden;
      border-radius: 8px;
      background: #15191E;
      color: #F3F5F7;
      border: 1px solid rgba(255, 255, 255, 0.12);
    }}
    .code-panel figcaption {{
      padding: 0.7rem 1rem;
      color: #A7B0BA;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      font-size: 0.82rem;
    }}
    pre {{
      margin: 0;
      padding: 1rem;
      overflow: auto;
      font: 0.95rem/1.55 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .code-line {{ display: block; white-space: pre; }}
    .image-panel {{ margin: 0; }}
    .image-panel img {{
      display: block;
      width: 100%;
      max-height: 58vh;
      object-fit: contain;
      border-radius: 8px;
    }}
    figcaption {{ color: var(--muted); margin-top: 0.5rem; }}
    .gallery {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
    }}
    .controls {{
      position: fixed;
      left: 50%;
      bottom: 1.5rem;
      display: flex;
      align-items: center;
      gap: 0.75rem;
      transform: translateX(-50%);
      color: var(--surface);
    }}
    .icon-button {{
      width: 2.5rem;
      height: 2.5rem;
      border: 1px solid rgba(255, 255, 255, 0.35);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.12);
      color: var(--surface);
      font-size: 1.5rem;
      cursor: pointer;
    }}
    .icon-button:focus-visible {{ outline: 3px solid var(--secondary); }}
    #counter {{
      min-width: 4.5rem;
      text-align: center;
      font-variant-numeric: tabular-nums;
    }}
    .meta {{
      position: fixed;
      right: 1.5rem;
      bottom: 1.9rem;
      color: rgba(255, 255, 255, 0.62);
      font-size: 0.82rem;
    }}
    .animate-fade-in {{ opacity: 0; }}
    .animate-fade-in.is-visible {{
      opacity: 1;
      transition: opacity var(--duration, 450ms) ease var(--delay, 0ms);
    }}
    .animate-slide-in {{ opacity: 0; transform: translateY(1rem); }}
    .animate-slide-in.is-visible {{
      opacity: 1;
      transform: translateY(0);
      transition:
        opacity var(--duration, 450ms) ease var(--delay, 0ms),
        transform var(--duration, 450ms) ease var(--delay, 0ms);
    }}
    .animate-line-reveal .reveal-line {{
      opacity: 0;
      transform: translateY(0.35rem);
    }}
    .animate-line-reveal.is-visible .reveal-line {{
      opacity: 1;
      transform: translateY(0);
      transition:
        opacity var(--duration, 220ms) ease calc(var(--delay, 0ms) + (var(--line-index, 0) * var(--stagger, 80ms))),
        transform var(--duration, 220ms) ease calc(var(--delay, 0ms) + (var(--line-index, 0) * var(--stagger, 80ms)));
    }}
    .animate-zoom {{ opacity: 0; transform: scale(0.96); }}
    .animate-zoom.is-visible {{
      opacity: 1;
      transform: scale(1);
      transition:
        opacity var(--duration, 450ms) ease var(--delay, 0ms),
        transform var(--duration, 450ms) ease var(--delay, 0ms);
    }}
    .animate-spin {{ transform: rotate(-2deg); }}
    .animate-spin.is-visible {{
      transform: rotate(0);
      transition: transform var(--duration, 450ms) ease var(--delay, 0ms);
    }}
    @media (max-width: 760px) {{
      .slide {{ padding: 1rem; }}
      .slide-inner {{ min-height: calc(100vh - 7rem); }}
      .split {{ grid-template-columns: 1fr; }}
      .meta {{ display: none; }}
      .controls {{ bottom: 0.85rem; }}
    }}"""


def _javascript() -> str:
    return """    const slides = Array.from(document.querySelectorAll('.slide'));
    const counter = document.querySelector('#counter');
    let current = 0;

    function selectedTarget(slide, target) {
      if (!target) return slide.querySelector('.slide-inner');
      try {
        return slide.querySelector(`[data-role="${target}"]`) || slide.querySelector('.slide-inner');
      } catch (e) {
        return slide.querySelector('.slide-inner');
      }
    }

    function prepareAnimations(slide) {
      const animations = JSON.parse(slide.dataset.animations || '[]');
      slide.querySelectorAll('[class*="animate-"]').forEach((node) => {
        node.classList.remove('animate-fade-in', 'animate-slide-in', 'animate-line-reveal', 'animate-zoom', 'animate-spin', 'is-visible');
      });
      animations.forEach((animation) => {
        const target = selectedTarget(slide, animation.target);
        target.classList.add(`animate-${animation.type}`);
        target.style.setProperty('--duration', `${animation.duration || 450}ms`);
        target.style.setProperty('--delay', `${animation.delay || 0}ms`);
        target.style.setProperty('--stagger', `${animation.stagger || 80}ms`);
      });
      requestAnimationFrame(() => {
        animations.forEach((animation) => {
          selectedTarget(slide, animation.target).classList.add('is-visible');
        });
      });
    }

    function showSlide(index) {
      current = (index + slides.length) % slides.length;
      slides.forEach((slide, slideIndex) => {
        slide.classList.toggle('active', slideIndex === current);
      });
      counter.textContent = `${current + 1} / ${slides.length}`;
      prepareAnimations(slides[current]);
    }

    document.querySelector('#prev').addEventListener('click', () => showSlide(current - 1));
    document.querySelector('#next').addEventListener('click', () => showSlide(current + 1));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowLeft') showSlide(current - 1);
      if (event.key === 'ArrowRight' || event.key === ' ' || event.key === 'Enter') showSlide(current + 1);
    });
    showSlide(0);"""


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="presentation JSON file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("index.html"),
        help="output HTML path (default: index.html)",
    )
    parser.add_argument(
        "--max-kb",
        type=int,
        default=500,
        help="fail if generated HTML exceeds this size (default: 500)",
    )
    args = parser.parse_args(argv)

    deck = load_presentation(args.input)
    output = render_html(deck)
    encoded = output.encode("utf-8")
    limit = args.max_kb * 1024
    if len(encoded) > limit:
        raise SystemExit(
            f"generated HTML is {len(encoded)} bytes, above {args.max_kb}KB limit"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    print(f"wrote {args.output} ({len(encoded)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
