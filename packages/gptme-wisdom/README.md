# gptme-wisdom

Local full-text search over reference books for gptme agents. Ingest
freely-licensed textbooks (SICP, OSTEP, Pro Git, …) into a SQLite FTS5 index,
then query them with BM25 keyword search from the CLI, from Python, or
automatically as session context. Every result carries chapter and section
provenance and the book's license for citation.

**Status:** beta. Small and dependency-light (only `click`), with a stable CLI.

## Why

LLM training data skews toward recent blog summaries rather than primary
sources. Classic textbooks hold foundational knowledge that rarely appears
verbatim in web crawls. A local book index gives dense, citable signal that
complements web search and session memory, and search never leaves your
machine.

Related packages:

- [gptme-wisdom-mcp](../gptme-wisdom-mcp/README.md) exposes the same kind of
  index (plus past agent sessions) as an MCP server for gptme, Claude Code,
  Claude Desktop and other MCP clients.
- [gptme-rag](../gptme-rag/README.md) provides vector/semantic search over your
  own documents. Use it for workspaces and gptme-wisdom for reference books.

## Install

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-wisdom"
# or
pipx install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-wisdom"
```

From a gptme-contrib checkout: `uv sync --package gptme-wisdom`.

With gptme ≥ 0.32 installed, the `gptme-wisdom` executable is also available as
the `gptme wisdom` subcommand.

## Quickstart

```bash
# 1. Get a freely-licensed book as plain text (example: Think Python, via Ghostscript)
curl -L https://greenteapress.com/thinkpython2/thinkpython2.pdf | \
  gs -sDEVICE=txtwrite -sOutputFile=- -q - > thinkpython.txt

# 2. Ingest. Curated slugs autofill title, URL and license
gptme-wisdom ingest --source thinkpython --file thinkpython.txt

# 3. Search
gptme-wisdom search "recursion base case"
gptme-wisdom search "virtual memory" --source ostep --limit 3

# 4. Manage the index
gptme-wisdom list
gptme-wisdom remove thinkpython
```

Input can be any plain-text or Markdown dump of a book.

## Commands

| Command | Key options |
|---|---|
| `ingest` | `--file PATH`, `--source SLUG` (required); `--title`, `--url`, `--license` (override curated metadata); `--target-tokens` (1000), `--overlap-tokens` (100), `--min-chunk-tokens` (50) |
| `search [QUERY]` | `--source SLUG`, `--limit` (5), `--json`, `--snippet-chars` (280), `--context` (emit a gptme context block), `--prompt-env` (read the query from `GPTME_PROMPT_INITIAL`) |
| `list` | `--json` |
| `remove SOURCE` | `--yes` (skip confirmation) |
| `context-cmd` | `--limit` (3), `--source SLUG`, `--toml` |

The index lives at `~/.local/share/gptme/wisdom.db`. To use another database,
pass `--db PATH` **before** the subcommand, e.g.
`gptme-wisdom --db ./books.db search "closures"`.

## Curated sources

These slugs autofill title, URL and license metadata. For any other book, pass
`--title` (and optionally `--url` and `--license`).

| Slug | Book | License |
|------|------|---------|
| `sicp` | Structure and Interpretation of Computer Programs | CC BY-SA 4.0 |
| `ostep` | Operating Systems: Three Easy Pieces | free (author-hosted) |
| `rl-intro` | Reinforcement Learning: An Introduction (2nd ed.) | free (author-hosted) |
| `thinkpython` | Think Python | CC BY-NC 3.0 |
| `mml-book` | Mathematics for Machine Learning | CC BY-NC-SA 4.0 |
| `pro-git` | Pro Git | CC BY-NC-SA 3.0 |
| `eloquentjs` | Eloquent JavaScript | CC BY-NC 3.0 |

## Automatic context in gptme sessions

To inject wisdom relevant to the first prompt of every new session, generate a
ready-to-paste `gptme.toml` snippet:

```bash
gptme wisdom context-cmd --toml
# [prompt]
# context_cmd = "gptme wisdom --db ... search --context --limit 3 --prompt-env"
```

The generated `context_cmd` reads the prompt from `GPTME_PROMPT_INITIAL`
(gptme ≥ 0.33) through the process environment, never by interpolating
untrusted text into a shell command. Use `--limit` and `--source` to tune it.
For one-off use: `gptme wisdom search --context "amortized complexity"`.

## Python API

```python
from gptme_wisdom import BookIndex, parse_book_text

docs = parse_book_text(text, source="mybook", title="My Book", url="https://example.com/book")
with BookIndex() as idx:          # default DB path; BookIndex(Path(...)) for another
    idx.add_many(iter(docs))
    results = idx.search("tail call optimization", limit=5)
```

Also exported: `BookDocument`, `DEFAULT_DB_PATH`, `estimate_tokens`.
