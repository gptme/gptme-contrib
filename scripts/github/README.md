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

### activity-gate.sh

Lightweight pre-check that scans GitHub for actionable activity before spawning an
expensive LLM session. Uses state-tracked timestamps to avoid re-reporting old items.

Without this gate, monitoring services can waste hundreds of LLM sessions per week on
empty runs. In Bob's deployment, this gate prevents ~97% of NOOP monitoring runs.

**Checks performed:**
- PR updates (new comments, reviews — excluding self-authored)
- CI failures (state-change tracked, dedup by conclusion hash)
- Merge conflicts (always reported until resolved)
- Merge readiness (CLEAN status + acceptable Greptile score)
- GitHub notifications (filtered for actionable reasons)
- Greptile score sweep (finds low-scored PRs needing code fixes)
- Assigned issues (issues assigned to the configured author)
- Master branch CI failures (CI regressions on the main branch)

**Exit codes:**
- `0` = actionable work found (items printed to stdout)
- `1` = no actionable work found
- `2` = usage error

**Usage:**
```bash
# Basic usage
activity-gate.sh --author MyBot --org myorg

# With extra repos outside the org
activity-gate.sh --author MyBot --org myorg --repo OtherOrg/repo

# JSONL output for structured processing
activity-gate.sh --author MyBot --org myorg --format jsonl

# Custom state directory (default: /tmp/github-activity-gate-state)
activity-gate.sh --author MyBot --org myorg --state-dir /tmp/my-state

# Use as a gate before spawning a session
if work=$(./activity-gate.sh --author MyBot --org myorg); then
    echo "Work found, spawning session..."
    echo "$work"
else
    echo "Nothing to do, skipping."
fi
```

**State tracking:**
Each check type uses files in the state directory to remember what it last reported.
On first run, all items are seeded (state created) but NOT reported — only changes
after the first run trigger output. This prevents a flood of items on initial setup.

**Integration pattern** (project monitoring):
```bash
# In your monitoring script:
ACTIVITY_GATE="$WORKSPACE/gptme-contrib/scripts/github/activity-gate.sh"
STATE_DIR="/tmp/my-agent-monitoring-state"

# Use a pending state dir so crashes don't lose state
PENDING_STATE_DIR="${STATE_DIR}-pending"
mkdir -p "$STATE_DIR" "$PENDING_STATE_DIR"
rsync -a --delete "$STATE_DIR/" "$PENDING_STATE_DIR/"

work=$("$ACTIVITY_GATE" --author "$AUTHOR" --org "$ORG" \
    --state-dir "$PENDING_STATE_DIR" --format jsonl) || {
    echo "No work found, skipping session."
    exit 0
}

# Promote state only after successful session — uncomment after your session call:
# rsync -a "$PENDING_STATE_DIR/" "$STATE_DIR/"
# Without this, all items re-emit every run (state is never committed).
```

**Requirements:**
- `gh` (GitHub CLI) installed and authenticated
- `jq` for JSONL output parsing

### greptile-helper.sh

Safe Greptile review trigger with anti-spam guards (flock, age guard, max re-triggers,
fail-safe cooldown). Always use this instead of posting raw `@greptileai review` comments.

**Usage:**
```bash
# Trigger a review (safe — checks guards first)
bash scripts/github/greptile-helper.sh trigger OWNER/REPO PR_NUMBER

# Check review status
bash scripts/github/greptile-helper.sh status OWNER/REPO PR_NUMBER
```

**Requirements:**
- `gh` (GitHub CLI) installed and authenticated
- Greptile GitHub app installed on the target repository

### check-notifications.sh

Fetch and format unread GitHub notifications for agent context injection.

**Usage:**
```bash
./scripts/github/check-notifications.sh
```

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

4. **Project Monitoring Gate:** Use activity-gate.sh to prevent wasted LLM sessions:
   ```bash
   # In your monitoring timer/service script
   if work=$(./gptme-contrib/scripts/github/activity-gate.sh \
       --author MyAgent --org myorg --format jsonl); then
       # Spawn LLM session with $work as context
       echo "$work" | your-agent-harness
   fi
   ```

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
