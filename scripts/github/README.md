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

### pr-greptile-trigger.py

Batch-trigger safe Greptile re-review requests for open PRs.

Scans open PRs authored by the authenticated user, identifies which ones need a
re-review (new commits since the last Greptile review), and routes triggers through
`greptile-helper.sh`. Never triggers initial reviews — Greptile auto-reviews new PRs.

**Features:**
- Safe re-review triggering (no spam — uses greptile-helper.sh guards)
- Configurable repo list via `GREPTILE_REPOS` env var or `--repo` flag
- Dry-run mode by default (use `--execute` to actually trigger)
- Status overview mode (`--status`)
- Filter PRs by author via `--author` (default: authenticated user)

**Usage:**
```bash
# Show what would be triggered (dry-run)
python3 scripts/github/pr-greptile-trigger.py

# Actually trigger re-reviews
python3 scripts/github/pr-greptile-trigger.py --execute

# Show review status for all open PRs
python3 scripts/github/pr-greptile-trigger.py --status

# Scan a specific repo
python3 scripts/github/pr-greptile-trigger.py --repo gptme/gptme

# Use custom repo list
GREPTILE_REPOS=myorg/repo1,myorg/repo2 python3 scripts/github/pr-greptile-trigger.py

# Filter by a specific author (useful in shared CI contexts)
python3 scripts/github/pr-greptile-trigger.py --author mybot
```

**Requirements:**
- `gh` (GitHub CLI) installed and authenticated
- `greptile-helper.sh` in the same directory

### self-merge-check.py

Evaluates whether a PR is eligible for autonomous agent self-merge.

Applies a conservative policy: CI must be green, Greptile must have reviewed with no
unresolved threads, and changed files must fall into a low-risk category (tests, docs,
lessons, internal tooling, task metadata). Sensitive/infra paths immediately disqualify.

**Features:**
- Auto-detects workspace repo from git remote (override with `--workspace-repo` or `WORKSPACE_REPO`)
- Cross-repo PRs disqualified when workspace repo is set (clear `WORKSPACE_REPO` to disable)
- JSON output mode for scripting
- Detailed per-check reasoning

**Usage:**
```bash
# Check a PR by URL
python3 scripts/github/self-merge-check.py https://github.com/owner/repo/pull/123

# Check by repo and number
python3 scripts/github/self-merge-check.py --repo gptme/gptme 456

# JSON output for scripting
python3 scripts/github/self-merge-check.py --json https://github.com/owner/repo/pull/123

# Allow cross-repo merges (clear workspace repo restriction)
WORKSPACE_REPO="" python3 scripts/github/self-merge-check.py https://github.com/gptme/gptme/pull/456

# Override workspace repo detection
python3 scripts/github/self-merge-check.py --workspace-repo myorg/myrepo <pr-url>
```

**Exit codes:** 0 = eligible, 1 = not eligible, 2 = error

**Requirements:**
- `gh` (GitHub CLI) installed and authenticated
- Greptile installed on the target repo for review checking

## Related

- [gptme](https://github.com/gptme/gptme) - The AI agent framework these scripts support
- [gptme-agent-template](https://github.com/gptme/gptme-agent-template) - Template for creating new agents
