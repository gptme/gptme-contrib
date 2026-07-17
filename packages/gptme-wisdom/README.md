# gptme-wisdom

BM25-searchable wisdom layer for gptme agents — index canonical reference books
and query them for foundational knowledge orthogonal to web search.

## Why

LLM training is recency-weighted toward blog summaries rather than primary
sources. Classic textbooks encode foundational knowledge that rarely appears
verbatim in web crawls. A local book index provides epistemically dense signal
that complements retrieval from session memory and the web.

## Install

```bash
uv pip install gptme-wisdom
# or
pip install gptme-wisdom
```

## Quick start

```bash
# 1. Download a freely-licensed book (example: Think Python)
curl -L https://greenteapress.com/thinkpython2/thinkpython2.pdf | \
  gs -sDEVICE=txtwrite -sOutputFile=- -q - > /tmp/thinkpython.txt

# 2. Ingest — curated slugs autofill title/url/license
gptme-wisdom ingest --source thinkpython --file /tmp/thinkpython.txt

# 3. Search (`gptme wisdom` delegates to `gptme-wisdom` on gptme >=0.32)
gptme wisdom search "recursion base case"
gptme wisdom search "virtual memory" --source ostep --limit 3

# 4. List indexed books
gptme-wisdom list

# 5. Remove a source
gptme-wisdom remove sicp
```

## Curated sources

These slugs autofill title, URL, and license metadata:

| Slug | Book | License |
|------|------|---------|
| `sicp` | Structure and Interpretation of Computer Programs | CC BY-SA 4.0 |
| `ostep` | Operating Systems: Three Easy Pieces | free (author-hosted) |
| `rl-intro` | Reinforcement Learning: An Introduction | free (author-hosted) |
| `thinkpython` | Think Python | CC BY-NC 3.0 |
| `mml-book` | Mathematics for Machine Learning | CC BY-NC-SA 4.0 |
| `pro-git` | Pro Git | CC BY-NC-SA 3.0 |
| `eloquentjs` | Eloquent JavaScript | CC BY-NC 3.0 |

For other books, pass `--title` (and optionally `--url`, `--license`).

## gptme context integration

On gptme >=0.32, the installed `gptme-wisdom` executable is automatically
available as the `gptme wisdom` external subcommand:

```bash
gptme wisdom search --context "amortized complexity"
```

To retrieve wisdom relevant to the first prompt of every new session, generate
a ready-to-paste `gptme.toml` snippet:

```bash
gptme wisdom context-cmd --toml
```

The generated `[prompt].context_cmd` uses `GPTME_PROMPT_INITIAL`, available in
gptme >=0.33, to pass the prompt through the process environment rather than
interpolating untrusted text into a shell command. `--limit` defaults to 3, and
`--source` can restrict retrieval to one book. Search stays entirely local.

## Storage

The index lives at `~/.local/share/gptme/wisdom.db` (SQLite FTS5). Override
with `--db PATH` on any command.

## API

```python
from gptme_wisdom import BookIndex, parse_book_text

docs = parse_book_text(text, source="mybook", title="My Book", url="https://...")
with BookIndex() as idx:
    idx.add_many(iter(docs))
    results = idx.search("tail call optimization")
```
