---
match:
  keywords:
    - "todoist"
    - "todoist mcp"
    - "todoist integration"
    - "todoist api"
    - "todoist token"
    - "TODOIST_API_TOKEN"
category: tools
---
# Todoist MCP Integration

## Rule
Use mcp-cli with the Todoist MCP server for task management. Two options: hosted endpoint (OAuth, simpler) or local server (API token).

## Context
When needing to create, read, update, or manage tasks in Todoist.

## Prerequisites

1. **mcp-cli** - Install with:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/philschmid/mcp-cli/main/install.sh | bash
   ```

2. **For local server**: Node.js/npx required
3. **For hosted endpoint**: Just authenticate via browser

## Setup Options

### Option 1: Hosted Endpoint (Recommended)

**URL**: `https://ai.todoist.net/mcp` (Streamable HTTP)

- No local setup required
- OAuth handles authentication automatically
- Works with Claude Desktop, Cursor, VS Code MCP extension

**Claude Desktop**: Settings → Connectors → Add custom connector → `https://ai.todoist.net/mcp`

**Claude CLI**:
```bash
claude mcp add --transport http todoist https://ai.todoist.net/mcp
```

### Option 2: Local Server (API Token)

**1. Get API Token**

Go to: https://todoist.com/app/settings/integrations/developer

Copy your personal API token.

**2. Set Environment Variable**

Add to your `.env` file:
```bash
TODOIST_API_TOKEN=your_api_token_here
```

**3. Create MCP Config**

```bash
mkdir -p ~/.config/mcp
cat > ~/.config/mcp/mcp_servers.json <<'EOF'
{
  "mcpServers": {
    "todoist": {
      "command": "npx",
      "args": ["-y", "@doist/todoist-ai"],
      "env": {
        "TODOIST_API_TOKEN": "${TODOIST_API_TOKEN}"
      }
    }
  }
}
EOF
```

**Note**: If you already have a config file, merge the `todoist` entry into your existing `mcpServers` object.

## Usage

```bash
# Source your token (if using local server)
source .env

# List available tools
mcp-cli todoist

# Create a task
mcp-cli todoist/create_task '{"content": "Review PR #123", "priority": 4}'

# List tasks
mcp-cli todoist/get_tasks '{}'

# Complete a task
mcp-cli todoist/complete_task '{"task_id": "123456789"}'

# Search tasks
mcp-cli todoist/search_tasks '{"query": "review"}'
```

## Supported Operations

| Operation | Status | Notes |
|-----------|--------|-------|
| Task CRUD | ✅ Works | Create, read, update, complete tasks |
| Subtasks | ✅ Works | Create tasks under parent tasks |
| Projects | ✅ Works | List and manage projects |
| Labels | ✅ Works | Add/remove labels |
| Priorities | ✅ Works | 1 (normal) to 4 (urgent) |
| Filters | ✅ Works | `today`, `overdue`, `p1`, etc. |
| Search | ✅ Works | Text-based task search |
| Natural dates | ✅ Works | `tomorrow`, `next Monday at 2pm` |

## Key Tools

| Tool | Purpose |
|------|---------|
| `create_task` | Create a new task |
| `get_tasks` | List tasks (with optional filter) |
| `update_task` | Modify task content/due date/priority |
| `complete_task` | Mark task as complete |
| `delete_task` | Remove a task |
| `search_tasks` | Search tasks by query |
| `get_projects` | List all projects |

## Priority Levels

| Level | Meaning | Todoist Display |
|-------|---------|-----------------|
| 1 | Normal (default) | No flag |
| 2 | Medium | Blue flag |
| 3 | High | Orange flag |
| 4 | Urgent | Red flag |

## Outcome
Following this pattern enables:
- **Task creation**: Quick task capture with natural language dates
- **Task management**: Update, complete, or delete tasks
- **Organization**: Use projects, labels, and priorities
- **Search & filter**: Find specific tasks quickly

## Related
- [mcp-cli Token Efficiency](./mcp-cli-token-efficiency.md) - General mcp-cli usage

## Resources

- **Official SDK**: https://github.com/Doist/todoist-ai
- **API Docs**: https://developer.todoist.com/rest/v2/
- **MCP Registry**: `net.todoist/mcp`
