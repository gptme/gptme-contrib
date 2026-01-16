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
When starting any work that involves MCP tools. Use mcp-cli as your default approach for MCP interactions rather than relying on eager-loaded tool schemas. This prevents the ~3000 token overhead of loading all MCP schemas at startup.

## Detection
Proactive signals to use mcp-cli (before starting work):
- About to use MCP tools for a task
- MCP servers configured in your environment
- Need to call external services via MCP (filesystem, APIs, etc.)
- Exploring what MCP capabilities are available

Reactive signals (if you notice these, start using mcp-cli):
- Context window filling up with tool schemas
- Slow startup due to MCP server connections

## Pattern: Using mcp-cli

### Installation
```bash
# Quick install
curl -fsSL https://raw.githubusercontent.com/philschmid/mcp-cli/main/install.sh | bash

# Or via bun
bun install -g https://github.com/philschmid/mcp-cli
```

### Configuration
See [`dotfiles/.config/mcp/mcp_servers.json`](../../dotfiles/.config/mcp/mcp_servers.json) for the canonical template.

Run `dotfiles/install.sh` to symlink it to `~/.config/mcp/mcp_servers.json`.

### When to Use Each Command

**Quick discovery** - Find available tools without loading schemas:
```bash
mcp-cli              # List servers/tools (~10 tokens vs 3000 with eager-load)
mcp-cli filesystem   # Narrow to one server when you know which you need
```

**Targeted lookup** - Get specific schema only when needed:
```bash
mcp-cli grep "read*"           # Find tools by pattern first
mcp-cli filesystem/read_file   # Then load only the schema you'll use
```

**Direct execution** - Call tools without going through gptme:
```bash
mcp-cli filesystem/read_file '{"path": "./README.md"}'  # One-shot call
mcp-cli -j filesystem/read_file '{"path": "./file.txt"}'  # JSON for parsing
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

## Options for Agents

| Scenario | Option | Benefit |
|----------|--------|---------|
| Need to understand tool purposes | `-d` | Adds descriptions without full schema loading |
| Parsing output programmatically | `-j` | JSON output for reliable parsing in scripts |
| Extracting just the content | `-r` | Raw output without formatting overhead |
| Multiple config environments | `-c <path>` | Switch between dev/prod MCP configs |

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
