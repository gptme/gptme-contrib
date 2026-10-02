# gptme-lessons-mcp

An MCP server that exposes gptme's **lesson** library — keyword-triggered behavioral rules for
agents — to any MCP client (Claude Code, Cursor, Continue.dev, …), so the same hard-won lessons
apply across runtimes and sessions.

**Status:** alpha. Small, read-only stdio server; thin wrapper over gptme's own `LessonIndex` and
`LessonMatcher`.

## Why / when to use it

Agents forget why builds failed between sessions. gptme solves this with lessons: Markdown files
with `match.keywords` frontmatter that are injected into context when relevant
([gptme lessons docs](https://gptme.org/docs/lessons.html)). If you also run agents outside
gptme, this server lets them ask "which lessons apply to what I'm about to do?" and read the
lesson text, using exactly the same files and matching logic.

Related:

- [gptme-lessons-extras](../gptme-lessons-extras/README.md) — validate and maintain a lessons library
- [`lessons/`](../../lessons/) — shared lessons in this repo you can point the server at
- [gptme-wisdom-mcp](../gptme-wisdom-mcp/README.md) — a different MCP server, for searching
  reference books and session history

## Install

Not published on PyPI. Install from the repository (pulls in `gptme` and `mcp`):

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-lessons-mcp"
```

## Quickstart

```bash
# stdio server using gptme's default lesson directories
gptme-lessons-mcp

# only search specific directories (repeatable)
gptme-lessons-mcp --lessons-dir ~/my-agent/lessons --lessons-dir ~/shared/lessons
```

Register it with Claude Code:

```bash
claude mcp add gptme-lessons -- gptme-lessons-mcp --lessons-dir /path/to/lessons
```

Or in any client that uses an `mcpServers` JSON config:

```json
{
  "mcpServers": {
    "gptme-lessons": {
      "command": "gptme-lessons-mcp",
      "args": ["--lessons-dir", "/path/to/lessons"]
    }
  }
}
```

## Tools

| Tool | Returns |
|------|---------|
| `match_lessons(context, top_k=5)` | Lessons whose keywords match `context` (e.g. a task description or error output), best first, with `score`, `matched_by` and body (truncated to 2000 chars). `top_k` is clamped to 1–20 |
| `list_lessons(category=None, search=None)` | Lesson metadata (`path`, `title`, `category`, `description`, `keywords`, `status`), filtered by category and/or a case-insensitive text search |
| `get_lesson(path)` | One lesson with its body (up to 10,000 chars); `path` matches a substring of the lesson path, falling back to a title substring |
| `list_categories()` | Sorted list of lesson categories |

## Options and lesson discovery

| Flag | Description |
|------|-------------|
| `--lessons-dir DIR` | Lesson directory to search (repeatable). When given, **only** these directories are used |
| `--debug` | Enable debug logging |

Without `--lessons-dir`, the server uses gptme's default discovery, run from the server's working
directory: `lessons/`, `skills/`, `.gptme/lessons/`, `.gptme/skills/` and `.cursor/` in the current directory, the gptme config
directory (`~/.config/gptme/lessons`, `.../skills`), `~/.agents/{lessons,skills}` and `~/.claude/skills`,
directories in `GPTME_LESSONS_EXTRA_DIRS` (colon-separated), and `[lessons] dirs` from gptme
config. The exact set depends on your installed gptme version.

## Development

```bash
uv run pytest packages/gptme-lessons-mcp/tests/
```
