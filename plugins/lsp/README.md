# gptme-lsp

LSP (Language Server Protocol) integration plugin for gptme, providing code intelligence features like diagnostics, definitions, and references.

## Phase 1: Diagnostics (Current)

This initial release focuses on **diagnostics** - the most reliable and actionable LSP feature for AI assistants.

### Features

- **`lsp diagnostics <file>`** - Get errors/warnings for a file
- **`lsp status`** - Show available language servers
- **`lsp check`** - Run diagnostics on all changed files (git)
- **Post-save hook** - Automatically shows errors after saving files

### Supported Languages

| Language | Tool Required | Install Command |
|----------|--------------|-----------------|
| Python | pyright | `npm install -g pyright` |
| TypeScript/JavaScript | tsc | `npm install -g typescript` |

### Installation

```bash
# Install language servers
npm install -g pyright typescript

# Install the plugin
pip install -e plugins/lsp
```

### Usage

```bash
# Check a file for errors
lsp diagnostics src/myfile.py

# See available language servers
lsp status

# Check all changed files
lsp check
```

### Post-Save Hook

When enabled, the plugin automatically runs diagnostics after you save Python or TypeScript files:
> User: Save this file
> Assistant: [saves file]
> System: ⚡ **Auto-diagnostics** for `myfile.py`:
> ❌ 2 error(s) found:
>   Line 15: Cannot assign to "x" because it has type "str" (expected "int")
>   Line 23: Module has no attribute "foo"

## Configuration

The plugin auto-detects available language servers. To use custom servers, configure them in your `gptme.toml`:

```toml
# Enable the plugin
[[plugins]]
path = "~/.local/share/gptme/plugins/lsp"
```

## Future Phases

### Phase 2: Extended Capabilities (Planned)
- Go-to-definition
- Find all references
- Hover documentation
- Rename symbol

### Phase 3: Advanced Features (Planned)
- Auto-fix suggestions
- Refactoring support
- Multi-language session support

## Architecture

```txt
gptme_lsp/
├── __init__.py          # Plugin entry point
├── lsp_client.py        # LSP protocol client (for future phases)
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
