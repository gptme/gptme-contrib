# Context Scripts

Generic context generation scripts for gptme agents. These produce system prompt context (journal entries, workspace structure, git status, task status) that agents use at conversation start.

## Scripts

| Script | Purpose |
|--------|---------|
| `context.sh` | Main orchestrator — calls all component scripts |
| `context-journal.sh` | Journal entries (supports flat + subdirectory formats) |
| `context-workspace.sh` | Workspace tree structure |
| `context-git.sh` | Git status + recent commits (with truncation) |
| `build-system-prompt.sh` | Reads `gptme.toml` and builds a full system prompt |

## Usage

### From gptme (via `gptme.toml`)

Set `context_cmd` to point at the orchestrator:

```toml
[prompt]
context_cmd = "gptme-contrib/scripts/context/context.sh"
```

### From agent repos (symlinks)

Agents that include gptme-contrib as a submodule can symlink:

```bash
# In your agent repo
ln -s gptme-contrib/scripts/context/context.sh scripts/context.sh
ln -s gptme-contrib/scripts/context/context-journal.sh scripts/context-journal.sh
# etc.
```

Or point `context_cmd` directly at the submodule path.

### From Claude Code / other harnesses

Use `build-system-prompt.sh` to generate a full prompt including identity files:

```bash
./gptme-contrib/scripts/context/build-system-prompt.sh /path/to/agent
```

### Individual components

Each script accepts an optional `AGENT_DIR` argument:

```bash
./scripts/context/context-journal.sh /path/to/agent
./scripts/context/context-workspace.sh /path/to/agent
./scripts/context/context-git.sh /path/to/agent
```

If not provided, they default to `git rev-parse --show-toplevel` (the repo root). This makes them safe to call via symlink from any location within a git repo.

## Design

- **No agent-specific logic**: These scripts are generic and work for any gptme-based agent
- **Truncation by default**: `context-git.sh` truncates output to prevent prompt blowup (see gptme/gptme#1561)
- **Graceful degradation**: Missing directories (tasks/, projects/, etc.) are handled without errors
- **Both journal formats**: Supports legacy flat (`journal/2025-01-01-topic.md`) and subdirectory (`journal/2025-01-01/topic.md`)
