# gptme-warp-grep

Agentic code search plugin for gptme using [Morph's warp-grep](https://docs.morphllm.com/sdk/components/warp-grep/direct).

## Overview

Warp Grep is an AI-powered code search tool that intelligently explores codebases over multiple turns (up to 4) until it finds the relevant code for your query.

Unlike simple grep, it:
- Understands natural language queries
- Strategically navigates large codebases
- Returns contextually relevant code snippets with line numbers
- Handles conceptual queries like "how does authentication work?"

## Installation

```bash
# Install the plugin
pip install -e plugins/warp-grep

# Or with uv
uv pip install -e plugins/warp-grep
```

## Configuration

Set your Morph API key:

```bash
export MORPH_API_KEY="your-api-key"
```

Get an API key at [morphllm.com/dashboard](https://morphllm.com/dashboard).

## Usage

### In gptme

Add the tool to your gptme configuration:

```toml
# ~/.config/gptme/config.toml
[tools]
modules = ["gptme_warp_grep"]
```

Then use in conversations:

```python
# Search for specific code patterns
warp_grep("Find authentication middleware")

# Search in a specific repo
warp_grep("Find all API endpoints", "/path/to/project")

# Conceptual queries work too
warp_grep("How are database errors handled?")
```

### As a library

```python
from gptme_warp_grep import warp_grep_search

# Returns list of ResolvedFile objects
results = warp_grep_search(
    query="Find where JWT tokens are validated",
    repo_root="/path/to/project",
)

for file in results:
    print(f"=== {file.path} ===")
    print(file.content)
```

## How it Works

1. **Query Analysis**: The model classifies your query (specific/conceptual/exploratory)
2. **Strategic Search**: Uses parallel grep, analyse, and read operations
3. **Iterative Refinement**: Up to 4 turns of searching and narrowing down
4. **Context Extraction**: Returns relevant code snippets with line numbers

### Available Tools (used internally)

- `grep '<pattern>' <path>` - Ripgrep search for regex patterns
- `read <path>[:start-end]` - Read file contents with optional line range
- `analyse <path> [pattern]` - List directory structure
- `finish <file:ranges>` - Return final code snippets

## Requirements

- Python 3.10+
- `ripgrep` (rg) installed and in PATH
- MORPH_API_KEY environment variable

## License

MIT
