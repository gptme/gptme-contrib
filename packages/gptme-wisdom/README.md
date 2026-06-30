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

# 3. Search
gptme-wisdom search "recursion base case"
gptme-wisdom search "virtual memory" --source ostep --limit 3

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

Use `--context` to emit results in gptme's context-block format — suitable for
piping into `context_cmd`:

```bash
# In gptme.toml:
# [context]
# context_cmd = "gptme-wisdom search --context '${QUERY}'"
gptme-wisdom search --context "amortized complexity"
```

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
