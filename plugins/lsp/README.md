# gptme-lsp

LSP (Language Server Protocol) integration plugin for gptme, providing code intelligence features like diagnostics, definitions, and references.

## Features

### Phase 1: Diagnostics ✅

- **`lsp diagnostics <file>`** - Get errors/warnings for a file
- **`lsp status`** - Show available language servers
- **`lsp check`** - Run diagnostics on all changed files (git)
- **Post-save hook** - Automatically shows errors after saving files

### Phase 2.1: Navigation ✅

- **`lsp definition <file:line:col>`** - Jump to symbol definition
- **`lsp references <file:line:col>`** - Find all references to a symbol
- **`lsp hover <file:line:col>`** - Get documentation and type information

### Phase 2.2: Refactoring Tools ✅

- **`lsp rename <file:line:col> <new_name>`** - Rename symbol across project

### Phase 2.3: User Experience ✅

- **Config file support** - Custom language server paths via `gptme.toml`
- **Better error messages** - Helpful hints when servers not found or fail
- **Lazy initialization** - Servers start only when first needed

### Phase 3: Advanced Features ✅

- **`lsp actions <file:line:col>`** - Get available code actions
- **`lsp symbols [query]`** - Search workspace symbols

### Phase 4: Formatting & Assistance ✅

- **`lsp format <file>`** - Format document using LSP
- **`lsp signature <file:line:col>`** - Get function signature help

### Phase 5: Inlay Hints & Call Hierarchy ✅

- **`lsp hints <file> [start:end]`** - Get inlay hints (parameter names, types)
- **`lsp callers <file:line:col>`** - Find functions that call a symbol
- **`lsp callees <file:line:col>`** - Find functions called by a symbol

### Phase 6: Advanced Analysis ✅ (NEW)

- **`lsp tokens <file> [start:end]`** - Get semantic tokens for syntax highlighting info
- **`lsp links <file>`** - Find document links (URLs, file paths)
- **`lsp lens <file>`** - Get code lenses (actionable annotations like "5 references")

## Configuration

### Custom Language Servers

Configure custom servers in `gptme.toml` (project root) or `~/.config/gptme/config.toml` (user-level).

Uses the `[plugin.lsp]` namespace to integrate with gptme's existing config system:

```toml
[plugin.lsp.servers]
# Override default server
python = ["pyright-langserver", "--stdio"]

# Use alternative server
python = ["pylsp"]

# Custom path
go = ["/custom/path/to/gopls", "serve"]

# Add new language
ocaml = ["ocamllsp"]
```

Project config (`gptme.toml`) overrides user config (`~/.config/gptme/config.toml`), which overrides built-in defaults.

### Lazy Initialization

By default, language servers start only when first needed:

```python
# Server starts on first command, not at LSPManager creation
manager = LSPManager(workspace)  # No servers started yet
manager.get_diagnostics(file)    # Python server starts now
manager.get_definition(file2)    # Server already running, reused

# Force eager initialization (previous behavior)
manager = LSPManager(workspace, lazy=False)  # All detected servers start
```


## Supported Languages

| Language | Server | Install |
|----------|--------|---------|
| Python | pyright | `npm i -g pyright` or `pipx install pyright` |
| TypeScript/JavaScript | typescript-language-server | `npm i -g typescript-language-server typescript` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |
| Rust | rust-analyzer | `rustup component add rust-analyzer` |
| C/C++ | clangd | System package manager |

## Installation

```bash
pip install gptme-lsp
```

Or install from source:

```bash
cd plugins/lsp
pip install -e .
```

## Usage

The LSP tool is automatically registered when the plugin is installed. Use the `lsp` command prefix:

```bash
# Check diagnostics for current file
gptme "lsp diagnostics src/main.py"

# Jump to definition
gptme "lsp definition src/main.py:42:5"

# Find all references
gptme "lsp references src/utils.py:15:10"

# Get hover information
gptme "lsp hover src/config.py:8:12"

# Rename a symbol across project
gptme "lsp rename src/utils.py:15:5 new_function_name"
```

## Development

```bash
# Run tests
cd plugins/lsp
make test

# Type check
make typecheck
```

## Roadmap

- [x] Phase 1: Diagnostics
- [x] Phase 2.1: Navigation (definition, references, hover)
- [x] Phase 2.2: Refactoring (rename)
- [x] Phase 2.3: User Experience (config files, error messages, lazy init)
- [x] Phase 3: Code Actions, Workspace Symbols
- [x] Phase 4: Formatting, Signature Help
- [x] Phase 5: Inlay Hints, Call Hierarchy
- [x] Phase 6: Semantic Tokens, Document Links, Code Lens
