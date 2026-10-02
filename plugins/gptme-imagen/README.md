# gptme-imagen — AI image generation for gptme and the terminal (Gemini, DALL-E)

Generate images from text prompts — diagrams, UI mockups, logos, illustrations —
with Google Gemini or OpenAI DALL-E, from inside a gptme conversation or with
the standalone `gptme-imagen` CLI. Generated images are saved to disk, can be
shown back to the model for review, and their estimated cost is logged locally.

**Status:** experimental. Usable; provider model names and prices are
hard-coded and may lag behind the providers.

## Why / when to use it

- Let an agent produce visual assets as part of a task (README diagrams,
  mockups, slide graphics) and **look at its own output** (`view=True`) to
  iterate.
- Compare providers on the same prompt, or generate several variants and pick.
- Use the same code from a shell script via the CLI, without gptme installed.

## Install

Not published on PyPI. Install from the repository subdirectory and pick the
extras you need:

```bash
# gptme plugin + both providers + CLI
pip install "gptme-imagen[all] @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=plugins/gptme-imagen"

# CLI only, Gemini only (no gptme dependency)
pip install "gptme-imagen[cli,gemini] @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=plugins/gptme-imagen"
```

Extras: `gptme` (plugin), `cli` (click), `gemini` (google-genai), `dalle`
(openai), `all`. Reference images additionally need Pillow, and DALL-E
URL responses need `requests`.

API keys are read from gptme's config/env lookup when gptme is installed,
otherwise from the environment:

```bash
export GOOGLE_API_KEY=...   # Gemini (GEMINI_API_KEY also accepted)
export OPENAI_API_KEY=...   # DALL-E 3 / DALL-E 2
```

### Enabling in gptme

When installed, the package registers a `gptme.plugins` entry point named
`gptme-imagen`, so the `image_gen` tool loads automatically (add `gptme-imagen`
to `[plugins] enabled` if you use an allowlist). From a gptme-contrib checkout
you can instead use:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-imagen"]
```

## Quickstart

### In gptme

The model writes `image_gen` blocks containing Python calls; results (with the
image attached) come back to the conversation:

```image_gen
generate_image(
    prompt="Architecture diagram of a three-tier web app",
    style="technical-diagram",
    output_path="docs/architecture.png",
    view=True,
)
```

### CLI

```bash
gptme-imagen generate "a sunset over mountains"
gptme-imagen generate "tech startup logo" -p dalle --quality hd --style flat-design -o logo.png
gptme-imagen generate "logo variations" -n 3   # generated_<timestamp>_001.png ... _003.png
gptme-imagen generate "change the background to a beach" -i photo.png   # Gemini only
gptme-imagen styles
gptme-imagen cost --since 2026-01-01
gptme-imagen history -n 20
```

`generate` options: `--provider/-p {gemini,dalle,dalle2}`, `--output/-o`,
`--size/-s`, `--quality/-q {standard,hd}`, `--count/-n`, `--style`,
`--enhance`, `--images/-i` (repeatable), `--open`. The CLI refuses an
`--output` that has a file extension when `--count` is greater than 1. For
numbered files such as `logos/option_001.png`, use the Python
`generate_image(..., output_path="logos/option.png", count=3)`.

## Functions (gptme tool)

| Function | Purpose |
|----------|---------|
| `generate_image(prompt, provider="gemini", size="1024x1024", quality="standard", output_path=None, count=1, view=False, style=None, enhance=False, show_progress=True, images=None)` | Generate one or `count` images; `images` = reference image path(s) for editing/multi-reference generation (Gemini only) |
| `generate_variation(image_path, provider="dalle2", count=1, size="1024x1024", output_path=None, view=False)` | Variations of an existing image (DALL-E 2 only) |
| `batch_generate(prompts, provider="gemini", output_dir=None, view=False, **kwargs)` | One image per prompt |
| `compare_providers(prompt, providers=None, view=True, **kwargs)` | Same prompt across providers |

- `provider`: `gemini` (model `gemini-3-pro-image-preview`), `dalle`
  (`dall-e-3`), `dalle2` (`dall-e-2`).
- `style` presets: `photo`, `illustration`, `sketch`, `technical-diagram`,
  `flat-design`, `cyberpunk`, `watercolor`, `oil-painting`.
- `enhance=True` appends generic quality/composition keywords to the prompt
  (template-based, no extra LLM call).
- Default output name is `generated_<timestamp>.png` in the current
  directory; with `count > 1`, `_001`, `_002`, ... suffixes are added.

## Cost tracking

Every generation is recorded in a local SQLite database at
`~/.gptme/imagen_costs.db`, with a cost **estimated** from a static per-image
price table (roughly $0.02–$0.08 per image; not reconciled with provider
billing). Query it with the CLI (`cost`, `history`) or from Python:

```python
from gptme_imagen.tools.image_gen import (
    get_total_cost, get_cost_breakdown, get_generation_history,
)
get_total_cost(provider="gemini", start_date="2026-01-01")
get_cost_breakdown()
get_generation_history(limit=10)
```
