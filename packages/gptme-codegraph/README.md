# gptme-codegraph

Structural code retrieval for gptme via [tree-sitter](https://tree-sitter.github.io/tree-sitter/) — complementary to gptme-rag (text chunks), this retrieves code *structure*: function/class definitions, call graphs, blast radius, and impact analysis.

## Features

- **5 MCP tools**: `codegraph_callers`, `codegraph_callees`, `codegraph_impact`, `codegraph_blast`, `codegraph_def`
- **Cross-file import resolution** (including `import X as Y`, `from X import Y as Z`, wildcard imports)
- **Qualified symbol IDs** (`module::Class.method`) for unambiguous cross-file references
- **SQLite index cache** — optional persistent cache for large codebases
- **Blast/impact semantics split**: `blast` = dependency closure (what X needs), `impact` = what breaks if you change X

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
from gptme_codegraph import build_index, impact_radius

index = build_index(Path("src/"))
callers, callees = impact_radius("my_module::MyClass.my_method", index)
```

## Status

Experimental package — Python only (v0). Multi-language support planned for v1.1.

> Namespace packages (`import google.cloud.storage` without `__init__.py`) are a known v1.1 gap.
