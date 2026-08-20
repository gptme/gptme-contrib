# Scripts

Standalone scripts and script collections for gptme agents and automation.
Most are runnable directly via `uv run` shebangs; see each directory's README
for details.

## Directories

| Directory | Description |
|-----------|-------------|
| [autoresearch/](./autoresearch/) | Autonomous research pipeline |
| [bluesky/](./bluesky/) | Bluesky integration |
| [claude-code-hooks/](./claude-code-hooks/) | Hooks for running agents on the Claude Code runtime |
| [context/](./context/) | Context generation for agent system prompts |
| [discord/](./discord/) | Discord bot integration |
| [git/](./git/) | Git workflow helpers (`git-safe-commit`, mass-delete guard, auto-stage hook) |
| [github/](./github/) | GitHub context generation, notifications, repo status |
| [github_actions_common/](./github_actions_common/) | Shared helpers for the GitHub Actions orchestrators (hygiene, resolver) |
| [github_hygiene/](./github_hygiene/) | Repo hygiene automation (labels, stale issues, etc.) |
| [github_resolver/](./github_resolver/) | Resolve GitHub issues/PRs to actionable context |
| [linear/](./linear/) | Linear issue tracking integration |
| [precommit/](./precommit/) | Pre-commit hook helpers |
| [runs/](./runs/) | Run/session management helpers |
| [status/](./status/) | Agent infrastructure status monitoring |
| [telegram/](./telegram/) | Telegram bot integration |
| [twitter/](./twitter/) | Twitter automation and monitoring |
| [workspace_validator/](./workspace_validator/) | Agent workspace structure validation |

## Notable standalone scripts

| Script | Description |
|--------|-------------|
| [agent-msg.py](./agent-msg.py) | Send messages between agents |
| [agent-write-loss-scan.py](./agent-write-loss-scan.py) | Detect silently-reverted writes in agent sessions (see below) |
| [check-claude-usage.sh](./check-claude-usage.sh) / [check-codex-usage.sh](./check-codex-usage.sh) / [check-openrouter-usage.sh](./check-openrouter-usage.sh) | Provider usage/quota checks |
| [check-quota.py](./check-quota.py) | Aggregate quota check across providers |
| [exa.py](./exa.py) | Web search via the Exa API |
| [fetch-community-plugins.py](./fetch-community-plugins.py) | Discover community plugins via GitHub topics |
| [fetch-github-trending.py](./fetch-github-trending.py) / [fetch-hn-top.py](./fetch-hn-top.py) | Trending/news feeds for agent context |
| [find-dupes.py](./find-dupes.py) | Find duplicate/near-duplicate files |
| [fleet_vitals.py](./fleet_vitals.py) | Health overview across an agent fleet |
| [gptme-heartbeat-validate.py](./gptme-heartbeat-validate.py) | Validate heartbeat protocol messages ([protocol docs](../docs/protocols/gptme-heartbeat.md)) |
| [perplexity.py](./perplexity.py) | Web search via the Perplexity API |
| [quota-gate.sh](./quota-gate.sh) | Gate session start on available quota |
| [search.py](./search.py) | Workspace-wide search |
| [state-status.py](./state-status.py) | Agent state/health status |
| [subscription-token-probe.py](./subscription-token-probe.py) | Probe subscription credential health |
| [vent.py](./vent.py) | Register friction/blockers to the shared friction ledger |
| [wordcount.py](./wordcount.py) | Word/token counting helper |

## agent-write-loss-scan

Detects the **write-loss (git-clobber) hazard** in gptme agent sessions: a session
writes to a file inside a git-tracked workspace, but the change is silently reverted
before it is ever committed — the write is lost without any error.

**Why this matters**: Autonomous agents write, but multi-session hot-windows can
revert those writes via `git stash`/`restore`/`checkout --` operations. Measured
write-loss rates in production: 0.051% overall, up to 0.68% for memory files.

**Usage**:

```bash
# Basic scan (human-readable)
python3 scripts/agent-write-loss-scan.py --repo /path/to/workspace

# JSON output for programmatic use
python3 scripts/agent-write-loss-scan.py --repo /path/to/workspace --json

# Filter by date, limit session count
python3 scripts/agent-write-loss-scan.py --repo /path/to/workspace \
  --since 2025-01-01 --limit 50
```

**Output fields** (JSON):

```json
{
  "total_writes": 142,
  "persisted": 138,
  "superseded": 2,
  "lost": 2,
  "unknown": 0,
  "loss_rate": 0.0141,
  "loss_pct": "1.4%",
  "events": [...]
}
```

**Classification**:

| Outcome | Meaning |
|---------|---------|
| `PERSISTED` | Written content was committed to git |
| `SUPERSEDED` | A later commit changed the file to different content (not a loss) |
| `LOST` | File reverted to pre-write state; content was never committed |
| `UNKNOWN` | Untracked path or ambiguous git history |

The scanner is strictly read-only: it never runs `git stash`, `checkout`,
`reset`, or `restore` — only `git log`, `cat-file`, and `ls-files`.
