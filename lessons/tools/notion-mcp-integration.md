---
match:
  keywords:
    - "notion"
    - "notion mcp"
    - "notion integration"
    - "notion api"
    - "notion token"
    - "NOTION_TOKEN"
category: tools
---
# Notion MCP Integration

## Rule
Use mcp-cli with the Notion MCP server for reading and writing Notion content. The server config is already in dotfiles - just set `NOTION_TOKEN`.

## Context
When needing to search, read, or write content in Notion workspaces.

## Prerequisites

1. **mcp-cli** - Install with:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/philschmid/mcp-cli/main/install.sh | bash
   ```

2. **Node.js/npx** - Required to run the Notion MCP server locally
   - The public MCP endpoint (`mcp.notion.com`) only works with Public integrations
   - Internal integrations require running the server locally via `npx`

3. **NOTION_TOKEN** - Get from Notion integrations page (see Setup below)

## Setup

### 1. Create Notion Integration

Go to: https://www.notion.so/profile/integrations/form/new-integration

**Choose integration type:**

| Type | Use Case | Auth Method |
|------|----------|-------------|
| **Internal** (recommended) | Private workspace access | API token |
| **Public** | Third-party apps others can connect to | OAuth |

For most agent use cases, choose **Internal**. This gives you a simple API token without OAuth complexity.

### 2. Copy the Token

After creating the integration, copy the "Internal Integration Secret" (starts with `ntn_` or `secret_`).

### 3. Set Environment Variable

Add to your `.env` file:
```bash
NOTION_TOKEN=ntn_your_token_here
```

### 4. Connect Pages to Integration

**Important**: The integration doesn't automatically have access to your workspace content.

For each page/database you want to access:
1. Open the page in Notion
2. Click `⋯` (more) menu → "Connect to" → Select your integration

## MCP Server Configuration

The config is already in `dotfiles/.config/mcp/mcp_servers.json`:
```json
{
  "mcpServers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "NOTION_TOKEN": "${NOTION_TOKEN}"
      }
    }
  }
}
```

After running `./dotfiles/install.sh`, this is symlinked to `~/.config/mcp/mcp_servers.json`.

## Usage

```bash
# Source your token
source .env

# Search workspace
mcp-cli notion/API-post-search '{"query": "meeting notes"}'

# Get page content (blocks)
mcp-cli notion/API-get-block-children '{"block_id": "page-id"}'

# List available tools
mcp-cli notion
```

## Permission Gotchas

### New Top-Level Pages Not Auto-Included

Even with full "Read content" permissions enabled:
- **New top-level pages** are NOT automatically accessible
- **Subpages** of already-connected pages ARE automatically included
- You must manually connect each new top-level page to the integration

**Workaround**: Create new pages as subpages of an already-connected parent page.

### Database Write Limitations

| Operation | Status | Notes |
|-----------|--------|-------|
| Read databases | ✅ Works | Query, list, get schema |
| Read pages/blocks | ✅ Works | Full content access |
| Update pages/blocks | ✅ Works | Modify content |
| **Update databases** | ❌ Blocked | Cannot add rows or modify schema |
| Create subpages | ✅ Works | Under connected pages |

**Database limitation**: The integration cannot write to databases (add rows, modify schema) even with all permission checkboxes enabled. This appears to be a Notion API limitation - the required permissions are not exposed in the web interface.

**Workaround**: Create pages instead of database entries, or use subpages for structured data.

## Available Tools

Key tools (run `mcp-cli notion -d` for full list with descriptions):

| Tool | Purpose |
|------|---------|
| `API-post-search` | Search across workspace |
| `API-get-block-children` | Get page content |
| `API-patch-block-children` | Append content to page |
| `API-get-database` | Get database schema |
| `API-post-database-query` | Query database rows |
| `API-get-user` | Get user info |

## Outcome
Following this pattern enables:
- **Workspace search**: Find pages and databases
- **Content reading**: Access page content and database entries
- **Content writing**: Update pages and append blocks
- **Structured data**: Query databases (read-only)

## Related
- [mcp-cli Token Efficiency](./mcp-cli-token-efficiency.md) - General mcp-cli usage
- [Notion Integration Research](/home/lofty/repos/lofty/knowledge/notion-integration-research.md) - Full research document
