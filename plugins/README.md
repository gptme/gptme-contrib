# gptme plugins

Plugins that add tools and hooks to [gptme](https://github.com/gptme/gptme): agent infrastructure, memory and retrieval, context and cost control, developer tools, voice and media, and analytics. All are experimental unless a plugin's README says otherwise. For the Python packages and the wider catalog, see the [top-level README](../README.md#catalog-by-category).

## Available plugins

"Package" is the Python package under `src/`; use it as the `[plugins] enabled` name when loading from a path. "Entry point" means the plugin registers a `gptme.plugins` entry point and loads automatically once installed into gptme's environment.

### Agent infrastructure

| Plugin | What it does | Package | Entry point |
|--------|--------------|---------|-------------|
| [gptme-gptodo](./gptme-gptodo/README.md) | Exposes the [gptodo](../packages/gptodo/README.md) task CLI as Python functions (`delegate`, `check_agent`, `list_tasks`, …) for coordinator agents that delegate to sub-agents | `gptme_gptodo` | yes |
| [gptme-gupp](./gptme-gupp/README.md) | Work-in-progress hooks (JSON files in `state/hooks/`) so an agent can resume after crashes, restarts or context compaction | `gptme_gupp` | no |
| [gptme-ralph](./gptme-ralph/README.md) | Ralph Loop: drive a spec + checkbox plan to completion one step at a time, with a fresh gptme or Claude Code context per step | `gptme_ralph` | yes |
| [gptme-action-receipts](./gptme-action-receipts/README.md) | Append-only, hashed audit ledger of every tool call, plus a `scope.yaml` allowlist for merges, force-pushes and repo/release deletes | `gptme_action_receipts` | yes |
| [gptme-hooks-examples](./gptme-hooks-examples/README.md) | Copyable template showing how to write and register lifecycle hooks (session start, tool pre-execute, turn post) | `gptme_example_hooks` | no |

### Memory, knowledge and retrieval

| Plugin | What it does | Package | Entry point |
|--------|--------------|---------|-------------|
| [gptme-retrieval](./gptme-retrieval/README.md) | Automatic RAG: before each step, queries qmd, [gptme-rag](../packages/gptme-rag/README.md), grep or a custom command and injects new, deduplicated matches | `gptme_retrieval` | yes |
| [gptme-user-memories](./gptme-user-memories/README.md) | ChatGPT-style long-term memory: extracts facts about the user at session end into a local Markdown file (plus a backfill CLI) | `gptme_user_memories` | yes |
| [gptme-ace](./gptme-ace/README.md) | Agentic Context Engineering for lessons: hybrid keyword + semantic retrieval, embedding dedup, and a pipeline that turns session logs into reviewable lesson deltas | `gptme_ace` | yes |
| [gptme-attention-tracker](./gptme-attention-tracker/README.md) | Keyword-activated HOT/WARM/COLD attention scores with decay for workspace files, plus per-turn context-usage history | `gptme_attention_tracker` | no |

### Context and cost management

| Plugin | What it does | Package | Entry point |
|--------|--------------|---------|-------------|
| [gptme-tooloutput-trimmer](./gptme-tooloutput-trimmer/README.md) | Trims (or LLM-summarizes) old oversized tool outputs before each request, only when the prompt cache is likely cold or context is under pressure. Opt-in. | `tooloutput_trimmer` | yes |
| [gptme-headroom-compressor](./gptme-headroom-compressor/README.md) | Losslessly compresses large JSON/tabular tool outputs with headroom-ai's SmartCrusher before they reach the model. Opt-in. | `headroom_compressor` | yes |

### Developer tools and integrations

| Plugin | What it does | Package | Entry point |
|--------|--------------|---------|-------------|
| [gptme-lsp](./gptme-lsp/README.md) | LSP diagnostics, definition/references/hover, call hierarchy and rename/format previews via pyright, typescript-language-server, gopls and rust-analyzer | `gptme_lsp` | no |
| [gptme-warpgrep](./gptme-warpgrep/README.md) | Natural-language code search with Morph's warp-grep model, which drives local ripgrep/read/list operations (needs a Morph API key) | `gptme_warp_grep` | no |
| [gptme-claude-code](./gptme-claude-code/README.md) | Delegate analyze/ask/fix/implement tasks to the Claude Code CLI (`claude -p`), synchronously or in background tmux sessions | `gptme_claude_code` | yes |
| [gptme-consortium](./gptme-consortium/README.md) | Ask several LLMs the same question, then have an arbiter model synthesize one answer with a confidence score | `gptme_consortium` | no |

### Voice and media

| Plugin | What it does | Package | Entry point |
|--------|--------------|---------|-------------|
| [gptme-tts](./gptme-tts/README.md) | Reads replies aloud sentence by sentence as they stream, via a bundled local server (Kokoro, KittenTTS, Chatterbox) or OpenRouter speech models | `gptme_tts` | yes |
| [gptme-imagen](./gptme-imagen/README.md) | Text-to-image (Gemini, DALL-E 3/2): `image_gen` tool, `gptme-imagen` CLI, style presets, view-back to the model, local cost tracking | `gptme_imagen` | yes |
| [gptme-youtube](./gptme-youtube/README.md) | Fetch a YouTube video's transcript from a URL or ID and summarize it | `gptme_youtube` | yes |

### Analytics

| Plugin | What it does | Package | Entry point |
|--------|--------------|---------|-------------|
| [gptme-wrapped](./gptme-wrapped/README.md) | Spotify-Wrapped-style yearly stats from local conversation logs: tokens, costs, top models, cache efficiency, activity heatmap | `gptme_wrapped` | yes |

## Installation

**Install into gptme's environment** (plugins with an entry point load automatically):

```bash
pipx inject gptme "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-tts"
# or, if gptme is installed with pip in the current environment:
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-tts"
```

If you use an allowlist, add the plugin's entry-point name or package name to `[plugins] enabled`.

**Or load from a checkout** by pointing `paths` at the plugin's directory in `gptme.toml` (project) or the gptme user config. This works for every plugin whose package has a `tools/`, `hooks/` or `commands/` subpackage, including those without an entry point (`gptme-ace`, `gptme-retrieval` and `gptme-tts` have none, so install those instead):

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-lsp"]
enabled = ["gptme_lsp"]   # optional allowlist; omit or leave empty to enable everything discovered
```

Pointing `paths` at the whole `plugins/` directory also discovers the tool plugins, but the allowlist then matches directory names (`gptme-lsp`) rather than package names.

Plugin dependencies, extras and config keys differ. Each plugin's README has its exact install line and settings.

## Plugin layout and naming

Each plugin is a src-layout Python package and a member of this repo's uv workspace:

```text
plugins/gptme-<name>/
├── pyproject.toml   # package name gptme-<name>; optional [project.entry-points."gptme.plugins"]
├── README.md
├── src/<package>/   # usually gptme_<name>
└── tests/
```

The directory and distribution name use `gptme-<name>`. The Python package is usually `gptme_<name>`, with a few exceptions (`gptme_example_hooks`, `gptme_warp_grep`, `headroom_compressor`, `tooloutput_trimmer`); the tables above list the actual names.

To write your own, start from [gptme-hooks-examples](./gptme-hooks-examples/README.md) and the [plugin-development skill](../skills/plugin-development/), and see the [gptme plugin docs](https://gptme.org/docs/plugins.html). Testing conventions are in [README_TESTS.md](./README_TESTS.md).
