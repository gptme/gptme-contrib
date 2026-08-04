# gptme-lessons-mcp

MCP server that exposes gptme's lesson matching system to any MCP client (Claude Code, Cursor, Continue.dev, etc.).

## What it does

Agents forget why builds fail between sessions. gptme-lessons-mcp makes lesson knowledge persistent and cross-runtime by exposing it as an MCP server.

## Tools

- `match_lessons(context, top_k=5)` — find lessons relevant to a context string
- `list_lessons(category?, search?)` — enumerate available lessons
- `get_lesson(path)` — get the full body of a specific lesson
- `list_categories()` — list all lesson categories

## Usage

```bash
# stdio MCP server (auto-discovers lessons from gptme config)
uv run gptme-lessons-mcp

# with explicit lesson directories
uv run gptme-lessons-mcp --lessons-dir ~/my-agent/lessons
```
