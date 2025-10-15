# GitHub Integration Scripts

Scripts for integrating GitHub context and repository management into AI agent workflows.

## Scripts

### context-gh.sh

Generates comprehensive GitHub context for AI agent conversations, reducing the need for manual tool calls during autonomous operation.

**Features:**
- GitHub notifications (unread, with type and reason)
- Open issues in agent workspace repository
- Multi-repository CI status checking
- Open PRs across all repositories
- Recent PRs in current repository

**Usage:**
```bash
./scripts/github/context-gh.sh
```

**Requirements:**
- `gh` (GitHub CLI) installed and authenticated
- Repository list configured in repo-status.sh

**Output:**
Markdown-formatted sections that can be included in agent context via gptme.toml's `context_cmd`.

### repo-status.sh

Check CI status across multiple repositories to catch failing builds before pushing changes.

**Features:**
- Quick status check for multiple repos
- Color-coded output (✓ passing, ✗ failing, ⚠ other states)
- Workflow URL display for failing builds
- Configurable repository list

**Usage:**
```bash
./scripts/github/repo-status.sh
```

**Configuration:**
Pass repositories as arguments (format: `owner/repo:Label` or just `owner/repo`):
```bash
./scripts/github/repo-status.sh gptme/gptme:gptme gptme/gptme-rag:gptme-rag
```

Or set `GH_USER` environment variable to customize user for PR checking:
```bash
export GH_USER=myusername
./scripts/github/repo-status.sh
```

Default behavior (no arguments): Checks gptme ecosystem repos.

**Requirements:**
- `gh` (GitHub CLI) installed and authenticated

## Integration with gptme

These scripts are designed to be integrated into gptme agent workflows:

1. **Dynamic Context:** Include context-gh.sh in your agent's main context.sh script:
   ```bash
   # In your agent's scripts/context.sh
   ./gptme-contrib/scripts/github/context-gh.sh
   ```

   Or use directly in gptme.toml (less common):
   ```toml
   context_cmd = "gptme-contrib/scripts/github/context-gh.sh"
   ```

2. **Pre-Push Checks:** Run repo-status.sh before pushing to ensure CI health:
   ```bash
   ./scripts/github/repo-status.sh && git push
   ```

3. **Autonomous Operations:** Include GitHub context automatically in agent prompts to reduce exploratory tool calls.

## Benefits

- **Reduced Tool Calls:** GitHub context is served upfront instead of requiring multiple API calls
- **Faster Decision Making:** Agent can see notifications, issues, and CI status immediately
- **Better Coordination:** Agent aware of open issues and PRs without manual checking
- **CI Health Awareness:** Catch failing builds before adding more changes

## Related

- [gptme](https://github.com/gptme/gptme) - The AI agent framework these scripts support
- [gptme-agent-template](https://github.com/gptme/gptme-agent-template) - Template for creating new agents
