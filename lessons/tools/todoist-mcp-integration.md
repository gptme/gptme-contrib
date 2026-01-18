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
3. **For hosted endpoint**: Node.js/npx required for `mcp-remote` proxy

## Setup Options

### Option 1: Hosted Endpoint with OAuth (Recommended)

**URL**: `https://ai.todoist.net/mcp` (Streamable HTTP)

The hosted endpoint uses OAuth authentication via `mcp-remote` proxy. First-time connection
opens your browser for Todoist authorization. No API token needed.

**1. Create MCP Config**

```bash
mkdir -p ~/.config/mcp
cat > ~/.config/mcp/mcp_servers.json <<'EOF'
{
  "mcpServers": {
    "todoist": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://ai.todoist.net/mcp"]
    }
  }
}
EOF
```

**2. First Connection (OAuth)**

```bash
# First run opens browser for OAuth authorization
mcp-cli todoist
```

Your browser will open to Todoist's authorization page. After approving, the OAuth token
is cached and subsequent calls work automatically.

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
# List available tools (24 tools available)
mcp-cli todoist

# Create a task
mcp-cli todoist/add-tasks '{"tasks": [{"content": "Review PR #123", "priority": "p1"}]}'

# Find tasks by date range
mcp-cli todoist/find-tasks-by-date '{"startDate": "today", "daysCount": 7}'

# Find tasks with filters
mcp-cli todoist/find-tasks '{"searchText": "review"}'

# Complete tasks
mcp-cli todoist/complete-tasks '{"ids": ["task-id-here"]}'

# Get user info
mcp-cli todoist/user-info '{}'

# Get overview (projects, tasks count)
mcp-cli todoist/get-overview '{}'
```

## Supported Operations

| Operation | Status | Notes |
|-----------|--------|-------|
| Task CRUD | ✅ Works | Create, read, update, complete tasks |
| Subtasks | ✅ Works | Create tasks under parent tasks |
| Projects | ✅ Works | List and manage projects |
| Labels | ✅ Works | Add/remove labels |
| Priorities | ✅ Works | p1 (urgent) to p4 (normal) |
| Filters | ✅ Works | `today`, `overdue`, `p1`, etc. |
| Search | ✅ Works | Text-based task search |
| Natural dates | ✅ Works | `tomorrow`, `next Monday at 2pm` |

## Key Tools

| Tool | Purpose |
|------|---------|
| `add-tasks` | Create new tasks (batch support) |
| `find-tasks` | Find tasks with filters (searchText, projectId, labels) |
| `find-tasks-by-date` | Find tasks by date range |
| `update-tasks` | Modify task content/due date/priority |
| `complete-tasks` | Mark tasks as complete (batch support) |
| `delete-object` | Remove a task (type: "task", id: "...") |
| `find-projects` | List all projects |
| `get-overview` | Get summary of projects and tasks |
| `user-info` | Get current user information |

## Priority Levels

| API Value | Meaning | Todoist Display |
|-----------|---------|-----------------|
| `p4` | Normal (default) | No flag |
| `p3` | Medium | Blue flag |
| `p2` | High | Orange flag |
| `p1` | Urgent | Red flag |

**Note**: Priority values are inverted - `p1` is highest priority (urgent), `p4` is lowest (normal).

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
