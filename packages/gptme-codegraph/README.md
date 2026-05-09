# gptme-codegraph

Structural code retrieval for gptme via [tree-sitter](https://tree-sitter.github.io/tree-sitter/) — complementary to gptme-rag (text chunks), this retrieves code *structure*: function/class definitions, call graphs, blast radius, and impact analysis.

## Features

- **9 MCP tools**: `codegraph_parse`, `codegraph_index`, `codegraph_map`, `codegraph_def`, `codegraph_callers`, `codegraph_callees`, `codegraph_refs`, `codegraph_blast`, `codegraph_impact`
- **Cross-file import resolution** (including `import X as Y`, `from X import Y as Z`, wildcard imports)
- **Qualified symbol IDs** (`module::Class.method`) for unambiguous cross-file references
- **SQLite index cache** — optional persistent cache for large codebases
- **Blast/impact semantics split**: `blast` = dependency closure (what X needs), `impact` = what breaks if you change X
- **Repo map / symbol skeletons** for token-cheap default codebase context

## Install

```bash
pip install gptme-codegraph[treesitter,mcp]
```

Or with uv:

```bash
uv add gptme-codegraph[treesitter,mcp]
```

## Usage

### CLI

```bash
# Extract symbols from a file
gptme-codegraph path/to/file.py parse

# Who calls a function?
gptme-codegraph path/to/file.py callers my_function

# What does a function call?
gptme-codegraph path/to/file.py callees my_function

# What breaks if you change a function?
gptme-codegraph path/to/file.py impact my_function

# Where is a symbol defined?
gptme-codegraph path/to/file.py def my_function

# Show a repo-map style symbol skeleton for a directory
gptme-codegraph path/to/repo map
```

### MCP Server

```bash
# Start the MCP server (stdio transport)
gptme-codegraph-mcp
```

Configure in Claude Code:

```bash
claude mcp add codegraph -- gptme-codegraph-mcp
```

### Python API

```python
from gptme_codegraph import (
    build_call_graph,
    build_cross_file_call_graph,
    build_index,
    extract_symbols,
    impact_radius,
)
from pathlib import Path

# Single-file: extract symbols and build call graph
symbols = extract_symbols(Path("src/my_module.py"))
_callees_graph, callers_graph = build_call_graph(symbols)

# Compute impact radius: what breaks if you change this symbol?
radius = impact_radius("my_function", callers_graph, max_depth=5)
print(radius)  # {"depth_0": {…}, "depth_1": {…}, …}

# Cross-file: build an index over a whole directory
index = build_index(Path("src/"))
_callees_graph, callers_graph = build_cross_file_call_graph(index, Path("src/"))
radius = impact_radius("my_module::MyClass.my_method", callers_graph, max_depth=5)
print(radius)  # {"depth_0": {…}, "depth_1": {…}, …}
```

## Status

Experimental package — Python only (v0). Multi-language support planned for v1.1.

> Namespace packages (`import google.cloud.storage` without `__init__.py`) are a known v1.1 gap.
