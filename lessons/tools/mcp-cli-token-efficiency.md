---
match:
  keywords:
    - "mcp-cli"
    - "mcp cli"
    - "MCP token"
    - "MCP context"
    - "lazy MCP"
category: tools
---
# mcp-cli for Token-Efficient MCP Interactions

## Rule
Use `mcp-cli` to interact with MCP servers on-demand instead of loading all tool schemas into context at startup.

## Context
When working with MCP tools and wanting to reduce token consumption. Instead of eager-loading all MCP tool schemas (which can consume 3000+ tokens), use mcp-cli to query tools on-demand.

## Detection
Observable signals indicating mcp-cli would help:
- MCP tool schemas consuming significant context tokens
- Multiple MCP servers configured but rarely all used
- Need to call specific MCP tools without loading everything
- Want to explore available MCP tools interactively

## Pattern: Using mcp-cli

### Installation
```bash
# Quick install
curl -fsSL https://raw.githubusercontent.com/philschmid/mcp-cli/main/install.sh | bash

# Or via bun
bun install -g https://github.com/philschmid/mcp-cli
```

### Configuration
Create `mcp_servers.json` in current directory or `~/.config/mcp/`:
```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    },
    "deepwiki": {
      "url": "https://mcp.deepwiki.com/mcp"
    }
  }
}
```

### Common Commands
```bash
# List all available servers and tools (names only)
mcp-cli

# List with descriptions
mcp-cli -d

# Show tools for a specific server
mcp-cli filesystem

# Show tool schema (input parameters)
mcp-cli filesystem/read_file

# Call a tool with arguments
mcp-cli filesystem/read_file '{"path": "./README.md"}'

# Search tools by pattern
mcp-cli grep "read*"

# JSON output for scripting
mcp-cli -j filesystem/read_file '{"path": "./file.txt"}'
```

### Token Efficiency Workflow
```bash
# Instead of loading 10 tools × 5 params = 50 schema entries at startup:

# 1. Discover what you need
mcp-cli grep "file"   # Find file-related tools

# 2. Check specific tool schema
mcp-cli filesystem/read_file   # Only load schema when needed

# 3. Call directly
mcp-cli filesystem/read_file '{"path": "./config.json"}'
```

## Anti-Pattern: Eager Loading
```text
# Current gptme MCP approach (expensive):
- Startup: Connect ALL servers
- Startup: Fetch ALL tool schemas
- Result: ~3000 tokens consumed before any work

# mcp-cli approach (efficient):
- Startup: No MCP overhead
- On-demand: Query specific tool when needed
- Result: ~100 tokens per tool used
```

## Options Reference
| Option | Description |
|--------|-------------|
| `-d, --with-descriptions` | Include tool descriptions |
| `-j, --json` | Output as JSON (for scripting) |
| `-r, --raw` | Output raw text content |
| `-c, --config <path>` | Custom config file path |

## Outcome
Following this pattern results in:
- **Reduced context usage**: Only load schemas when needed
- **Faster startup**: No eager MCP server connections
- **Shell integration**: Call MCP tools from any script
- **Exploration**: Easily discover available tools

## Related
- [Linear API Integration](./linear-api-integration.md) - Similar CLI-first pattern
- [gptme Issue #1123](https://github.com/gptme/gptme/issues/1123) - Lazy MCP loading proposal

## Origin
2026-01-16: Created based on Erik's suggestion in SUDO-51 to teach agents how to use mcp-cli for token-efficient MCP workflows. References loftybuilder's investigation summary.
