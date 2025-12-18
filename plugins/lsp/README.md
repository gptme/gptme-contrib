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

### Phase 2.2: Refactoring Tools ✅ (NEW)

- **`lsp rename <file:line:col> <new_name>`** - Rename symbol across project

## Supported Languages

| Language | Tool Required | Install Command |
|----------|--------------|-----------------|
| Python | pyright | `npm install -g pyright` |
| TypeScript/JavaScript | typescript-language-server | `npm install -g typescript-language-server typescript` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |
| Rust | rust-analyzer | `rustup component add rust-analyzer` |

## Installation

```bash
# Install language servers (pick the ones you need)
npm install -g pyright
npm install -g typescript-language-server typescript
go install golang.org/x/tools/gopls@latest
rustup component add rust-analyzer

# Install the plugin
pip install -e plugins/lsp
```

## Usage

### Diagnostics

```bash
# Check a file for errors
lsp diagnostics src/myfile.py

# Check all changed files
lsp check

# See available language servers
lsp status
```

### Navigation (Phase 2.1)

```bash
# Jump to definition of symbol at line 42, column 10
lsp definition src/myfile.py:42:10

# Find all references to symbol at line 15, column 5
lsp references src/utils.py:15:5

# Get documentation/type info for symbol
lsp hover src/config.py:8:12
```

### Refactoring (Phase 2.2)

```bash
# Rename a function across the entire project
lsp rename src/utils.py:15:5 new_function_name

# Rename a class
lsp rename src/models.py:10:7 NewClassName

# Rename a variable
lsp rename src/config.py:5:1 NEW_CONSTANT_NAME
```

**Note:** The rename command shows all proposed changes for preview. Use the patch tool to apply the edits.

### Post-Save Hook

When enabled, the plugin automatically runs diagnostics after you save files:
> User: Save this file
> Assistant: [saves file]
> System: ⚡ **Auto-diagnostics** for `myfile.py`:
> ❌ 2 error(s) found:
>   Line 15: Cannot assign to "x" because it has type "str" (expected "int")
>   Line 23: Module has no attribute "foo"

## Use Cases

1. **Better debugging**: Get real errors before running code
2. **Code navigation**: Find definitions without grepping
3. **Refactoring safety**: Find all references before changes
4. **Documentation access**: Quick hover for unfamiliar APIs
5. **Type information**: Understand function signatures

## Configuration

The plugin auto-detects available language servers. To use custom servers, configure them in your `gptme.toml`:

```toml
# Enable the plugin
[[plugins]]
path = "~/.local/share/gptme/plugins/lsp"
```

## Future Phases

### Phase 2.2: Refactoring Tools (Planned)
- Rename symbol across project
- Workspace-wide edits

### Phase 2.3: User Experience (Planned)
- Config file for custom language server paths
- Better error messages
- Performance: lazy server initialization

### Phase 3: Advanced Features (Planned)
- Auto-fix suggestions (code actions)
- Workspace symbols search
- Multi-language session support

## Architecture

```txt
gptme_lsp/
├── __init__.py          # Plugin entry point
├── lsp_client.py        # LSP protocol client with full protocol support
├── tools/
│   ├── __init__.py
│   └── lsp_tool.py      # LSP tool implementation
└── hooks/
    ├── __init__.py
    └── post_save.py     # Post-save diagnostics hook
```

## Development

```bash
# Install in development mode
pip install -e "plugins/lsp[test]"

# Run tests
pytest plugins/lsp/tests/
```

## References

- [LSP Specification](https://microsoft.github.io/language-server-protocol/)
- [OpenCode](https://opencode.ai/) - Inspiration for LSP integration patterns
- [mcp-language-server](https://github.com/isaacphi/mcp-language-server) - MCP-based alternative

## License

MIT
