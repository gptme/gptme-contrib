# gptme-retrieval

Automatic RAG for [gptme](https://gptme.org): before each LLM step, search your notes, docs or past conversations for content relevant to the latest user message and inject the best matches into context, without the agent having to ask.

**Status:** experimental.

## How it works

The plugin registers a `STEP_PRE` hook. Before every LLM step it:

1. Takes the text of the most recent user message as the query.
2. Runs the configured search backend ([qmd](https://github.com/tobi/qmd), [gptme-rag](../../packages/gptme-rag/README.md), `grep`, or your own command).
3. Drops results below the score threshold and deduplicates them per conversation (keyed by source path plus a content hash), so a document is injected at most once per conversation even across many tool-call steps.
4. Injects any new results as a system message headed `## Retrieved Context`.

The backend is queried on every step, so new documents surface as soon as the topic changes; only injection is deduplicated.

## Install

Install into the same Python environment as gptme. The package registers itself through the `gptme.plugins` entry point:

```sh
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-retrieval"
# or, for a pipx-installed gptme:
pipx inject gptme "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-retrieval"
```

Then install a backend. For the default `qmd` backend, follow the [qmd install instructions](https://github.com/tobi/qmd) and index something:

```sh
qmd collection add ~/notes --name notes
qmd embed   # needed for the default "vsearch" (semantic) mode
```

## Configuration

Settings live under `[plugin.retrieval]` in the project's `gptme.toml` or in `~/.config/gptme/config.toml`. The whole table is taken from the project config if present, otherwise from the user config; missing keys fall back to defaults.

```toml
[plugin.retrieval]
enabled = true          # default: true
backend = "qmd"         # "qmd" (default), "gptme-rag", "grep", or a custom command
mode = "vsearch"        # qmd only: "search" (BM25), "vsearch" (semantic, default), "query" (hybrid)
max_results = 5         # default: 5
threshold = 0.3         # drop results scoring below this; default: 0.3
collections = []        # qmd only: restrict to these collection names
inject_as = "system"    # "system" (visible, default) or "hidden"
```

## Backends

| Backend | Command run | Notes |
|---------|-------------|-------|
| `qmd` | `qmd <mode> <query> --json -n <max_results> [--collection <name> ...]` | Local BM25 / vector / hybrid search over indexed collections. |
| `gptme-rag` | `gptme-rag search <query> -n <max_results> --json` | Local ChromaDB semantic search; see [gptme-rag](../../packages/gptme-rag/README.md). |
| `grep` | `grep -r -l -i <query> .` | Fallback: lists matching file *names* in the current directory (no content). |
| custom | `<your command> <query> --mode <mode> -n <max_results>` | Must print a JSON list of objects with `content`, `source` and `score` keys. |

Each backend call times out after 10 seconds (15 for `gptme-rag`); failures are logged and the step continues without retrieved context.

> **Known issue:** current qmd releases emit `file` and `snippet` fields in `--json` output, while the plugin reads `content` and `path`/`source`. With such versions results may be injected with empty content. Check what your qmd version prints before relying on this backend, or use `gptme-rag` or a custom command.

## Indexing past conversations

gptme stores conversations as `conversation.jsonl` files under its logs directory (by default `~/.local/share/gptme/logs/`). To make them searchable, export the user and assistant messages to text files in a directory and add that directory as a qmd collection (or index it with `gptme-rag`).

## Related

- [gptme-rag](../../packages/gptme-rag/README.md): local semantic search as a CLI, gptme tool, or MCP server
- [gptme plugin docs](https://gptme.org/docs/plugins.html)
- [gptme/gptme#59](https://github.com/gptme/gptme/issues/59): the original feature discussion
