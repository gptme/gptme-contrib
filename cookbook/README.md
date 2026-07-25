# gptme Interactive Cookbook

A curated collection of canonical patterns for building with gptme. Each
pattern is a self-contained YAML+markdown file that covers a real problem,
the gptme solution, and a minimal working example.

## Patterns

| File | Pattern | Category | Difficulty |
|------|---------|----------|------------|
| [01-tool-use-basics.md](./01-tool-use-basics.md) | Tool Use Basics | tool-use | beginner |
| [02-parallel-research-pipeline.md](./02-parallel-research-pipeline.md) | Parallel Research Pipeline | multi-agent | intermediate |
| [03-persistent-context-with-skills.md](./03-persistent-context-with-skills.md) | Persistent Context with Skills | context-management | intermediate |
| [04-skill-composition-pipeline.md](./04-skill-composition-pipeline.md) | Compose Skills into Workflows | skill-composition | advanced |
| [05-custom-tool-plugin.md](./05-custom-tool-plugin.md) | Write a Custom Tool Plugin | custom-tools | advanced |

## Pattern Format

Each pattern file uses YAML frontmatter followed by markdown:

```yaml
---
title: Human-readable title
description: One-line description shown in the HTML index
category: tool-use | multi-agent | context-management | skill-composition | custom-tools
difficulty: beginner | intermediate | advanced
tags: [comma, separated, tags]
deep_link: "https://gptme.ai/?prompt=<url-encoded-prompt>"
---
```

Followed by markdown sections:

- `## Problem` — what was hard before (1–3 sentences)
- `## Solution` — how gptme addresses it
- `## Example` — minimal working code / commands
- `## Notes` — caveats, tips, related resources (optional)

## Building the HTML Index

Run the build script from the repo root:

```bash
python3 scripts/build-cookbook.py
# Output: cookbook/index.html
```

Options:

```bash
python3 scripts/build-cookbook.py --cookbook-dir cookbook --output cookbook/index.html
```

The generated `index.html` is a self-contained static page — no build toolchain
required. It can be served from any static host.

## Adding a Pattern

1. Create `cookbook/NN-my-pattern.md` with the frontmatter above.
2. Fill in the four standard sections (Problem / Solution / Example / Notes).
3. Set `deep_link` to a URL that opens gptme.ai with the example pre-loaded.
4. Run `python3 scripts/build-cookbook.py` to verify it renders correctly.
5. Submit a PR — the cookbook HTML is committed alongside the markdown.

## Deep-Link Format

The `deep_link` field should be a URL that pre-loads the pattern's example
prompt in gptme.ai. Current format:

```
https://gptme.ai/?prompt=<url-encoded-prompt>
```

If the gptme.ai URL scheme changes, update the `deep_link` fields and rebuild.
