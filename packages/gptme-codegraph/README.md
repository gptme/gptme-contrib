# gptme-codegraph

Structural code retrieval with [tree-sitter](https://tree-sitter.github.io/tree-sitter/):
"where is `X` defined?", "who calls `X`?", "what breaks if I change `X`?", and a
token-cheap repo map — as a CLI, a Python library, and an MCP server for gptme,
Claude Code, or any MCP client.

**Status:** experimental (`0.1.0`). Python support is the deepest path; other
languages get symbol extraction and best-effort cross-file resolution.

Complementary to [gptme-rag](../gptme-rag/README.md) (semantic search over text
chunks): codegraph retrieves code *structure* — definitions, call graphs,
dependency closure, and impact radius.

## Features

- **Multi-language symbol extraction** for Python, JavaScript (`.js/.jsx/.mjs/.cjs`),
  TypeScript (`.ts/.tsx`), Rust, Go, Java, C#, Ruby, C, C++, PHP, Kotlin, and Swift
- **Cross-file call graph** with qualified symbol IDs (`module::Class.method`);
  strongest on Python, with cross-module resolution also for TypeScript, Go, and Rust
- **Impact vs dependencies**: `impact` = what breaks if you change X (callers,
  upstream); `deps` = what X transitively depends on (callees, downstream)
- **Repo map / symbol skeletons** for cheap codebase context, plus a committed
  `.gptme-codegraph-map.json` artifact with freshness checks
- **Local lexical (BM25) symbol search** for "find the code that handles retries"
  when you don't know the name — no external APIs
- **SQLite index cache** so large repos are indexed once and reused

## When to use

Pick the retrieval tool by the *shape* of the question:

- **codegraph** — symbol questions: *where is `X` defined?*, *who calls `X`?*,
  *what breaks if I change `X`?*, *give me a repo skeleton*.
- **grep / ripgrep** — exact strings: a literal identifier, an error message, a
  config key.
- **semantic search** ([gptme-rag](../gptme-rag/README.md)) — conceptual queries
  over prose and code text: *how does auth work here?*

Rule of thumb: exact text → grep; "what does this concept look like" → semantic;
"how is this symbol wired" → codegraph.

## Install

Not published to PyPI. Install from the repository subdirectory with the
tree-sitter grammars (and the MCP server, if you want it):

```bash
uv tool install "gptme-codegraph[treesitter,mcp] @ git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-codegraph"
# or
pip install "gptme-codegraph[treesitter,mcp] @ git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-codegraph"
```

Extras: `treesitter` (grammars for all supported languages), `mcp` (MCP server).
This installs three commands: `gptme-codegraph`, `gptme-codegraph-mcp`, and
`gptme-codegraph-commit-map`.

## CLI

The first argument is a file or directory, followed by a subcommand:

```bash
gptme-codegraph src/app.py parse                  # list symbols in a file
gptme-codegraph src/app.py def my_function        # where is it defined?
gptme-codegraph src/app.py callers my_function    # who calls it?
gptme-codegraph src/app.py callees my_function    # what does it call?
gptme-codegraph src/app.py refs my_function       # references
gptme-codegraph src/app.py impact my_function     # what breaks if it changes?
gptme-codegraph src/app.py deps my_function       # what it depends on

# Cross-file: build an index over a directory (optionally cached in SQLite)
gptme-codegraph src/app.py --directory src/ --use-sqlite impact "app::Server.start"

# Repo map / symbol skeleton
gptme-codegraph . map --max-files 20 --max-symbols 12

# Concept search over indexed symbols (BM25, local)
gptme-codegraph . search "retry with backoff" --limit 10
```

Every subcommand accepts `--json`. `impact`, `deps`, and the deprecated `blast`
(use `impact` or `deps`) take `--max-depth` (default 10). Global options
(`--directory`, `--use-sqlite`) go before the subcommand. The SQLite cache lives
under `~/.local/state/codegraph/`.

## Committed repo-map artifact

"Analyze once, commit the graph." Generate `.gptme-codegraph-map.json` so
teammates and agents can read a repo's structural outline without re-running
tree-sitter:

```bash
gptme-codegraph-commit-map path/to/repo             # generate and save
gptme-codegraph-commit-map path/to/repo --check     # exit 0 = fresh, 1 = stale/missing
gptme-codegraph-commit-map path/to/repo --refresh   # regenerate only if stale
gptme-codegraph-commit-map path/to/repo --refresh --force   # always regenerate
```

Other options: `--output/-o` (default `.gptme-codegraph-map.json`),
`--max-files` (20), `--max-symbols-per-file` (12), `--stale-after-days` (1),
and `--no-cache` to bypass the stat-fingerprint cache in `~/.cache/gptme-codegraph/`.

Freshness is keyed off a digest of git-tracked `*.py`, `*.ts`, `*.tsx`, `*.js`,
and `*.rs` files, not `HEAD`, so an artifact regenerated in a pre-commit hook
stays fresh after the commit that contains it lands. The artifact is structural
only (paths, class/function names, nesting) — no source, comments, or values.

## MCP server

`gptme-codegraph-mcp` runs over stdio and exposes 10 tools:
`codegraph_parse`, `codegraph_index`, `codegraph_map`, `codegraph_def`,
`codegraph_callers`, `codegraph_callees`, `codegraph_refs`, `codegraph_search`,
`codegraph_impact` (upstream: what breaks), and `codegraph_blast` (downstream:
dependency closure). Most tools take either a `filepath` or a `directory` for
cross-file mode.

Claude Code:

```bash
claude mcp add codegraph -- gptme-codegraph-mcp
```

gptme (`~/.config/gptme/config.toml` or a project `gptme.toml`):

```toml
[mcp]
enabled = true

[[mcp.servers]]
name = "codegraph"
command = "gptme-codegraph-mcp"
```

## Python API

```python
from pathlib import Path
from gptme_codegraph import (
    build_call_graph,
    build_cross_file_call_graph,
    build_index,
    build_repo_map,
    dependency_closure,
    extract_symbols,
    format_repo_map,
    impact_radius,
)

# Single file
symbols = extract_symbols(Path("src/my_module.py"))
callees, callers = build_call_graph(symbols)
print(impact_radius("my_function", callers, max_depth=5))   # {"depth_0": {...}, "depth_1": {...}}
print(dependency_closure("my_function", callees, max_depth=5))

# Cross-file
index = build_index(Path("src/"))
callees, callers = build_cross_file_call_graph(index, Path("src/"))
print(impact_radius("my_module::MyClass.my_method", callers, max_depth=5))
```

Also exported: `SymbolIndex`, `SqliteIndexCache`, `parse_file`,
`build_repo_map` / `format_repo_map`, and the search types `LexicalScorer`,
`SearchDocument`, `SearchResult`, `extract_search_documents`.

## Known gaps

- Non-Python import resolution is best-effort, not fully semantic.
- Python namespace packages (no `__init__.py`) are not resolved yet.
- `search` has only the `lexical` backend.
