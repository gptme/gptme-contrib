# gptme-activity-summary

Daily, weekly and monthly work reports ("what did I get done?") for AI agents
and humans — built from journals, GitHub activity, agent session logs, posted
tweets, sent email, and ActivityWatch time tracking, with an LLM writing the
narrative.

**Status:** beta. Used daily to summarize an autonomous gptme agent's work;
human mode is newer and less exercised.

> **Supersedes [whatdidyougetdone](https://github.com/TimeToBuildBob/whatdidyougetdone)** —
> the human work-report use case lives here now, with ActivityWatch as the
> primary time-tracking source and GitHub as an optional overlay.

## Two modes

- **Agent mode** (default) — reads an agent workspace's `journal/`, plus GitHub
  activity, gptme and Claude Code session stats (via
  [gptme-sessions](../gptme-sessions/README.md), including per-model token/cost
  breakdowns), posted tweets and sent email. Summaries are saved as Markdown
  under `knowledge/summaries/{daily,weekly,monthly}/` in the workspace.
- **Human mode** (`--mode human`) — no agent workspace needed. Uses
  [ActivityWatch](https://activitywatch.net) as the primary source, with your
  GitHub activity as an optional overlay. If ActivityWatch isn't running, its
  data is skipped and GitHub-only reports still work.

Agent workspaces following the
[gptme-agent-template](https://github.com/gptme/gptme-agent-template) layout work
out of the box. The tweet and email sources read `tweets/posted/*.yml` and
`email/sent/*.md` — the latter is where [gptmail](../gptmail/README.md) stores
sent mail. Missing sources are skipped.

## Install

The package depends on other gptme-contrib workspace packages
(`gptme-sessions`), so install it from a gptme-contrib checkout:

```bash
git clone https://github.com/gptme/gptme-contrib
cd gptme-contrib
uv sync --all-packages          # whole workspace, or:
uv sync --package gptme-activity-summary
uv run gptme-activity-summary --help
```

The examples below assume the workspace venv is active (or prefix them with
`uv run`).

LLM summaries use the Claude Code CLI (`claude -p`), so `claude` must be
installed and logged in. Use `--raw` to skip the LLM in human mode. GitHub
data uses the `gh` CLI.

## Usage

### Agent mode (journal-based)

Run from the agent workspace (or set `GPTME_WORKSPACE`):

```bash
gptme-activity-summary daily --date yesterday
gptme-activity-summary weekly --week last
gptme-activity-summary monthly --month last

# Daily, plus weekly on Mondays and monthly on the 1st — good for a daily timer
gptme-activity-summary smart --date yesterday

# Regenerate a range of daily summaries
gptme-activity-summary backfill --from 2026-09-01 --to 2026-09-07

# Limit GitHub data to specific repos (repeatable)
gptme-activity-summary daily --repo gptme/gptme --repo gptme/gptme-contrib
```

### Human mode (ActivityWatch + optional GitHub)

```bash
gptme-activity-summary daily --mode human --date yesterday
gptme-activity-summary daily --mode human --date yesterday --github-user your-github-username
gptme-activity-summary weekly --mode human --week last --github-user your-github-username
gptme-activity-summary monthly --mode human --month last --github-user your-github-username

# Print the source data without LLM summarization
gptme-activity-summary weekly --mode human --week last --github-user your-github-username --raw
```

## Commands

| Command | Purpose | Key options |
|---------|---------|-------------|
| `daily` | One day | `--date` (`YYYY-MM-DD`, `today`, `yesterday`), `--mode`, `--github-user`, `--raw`, `--repo` |
| `weekly` | One ISO week | `--week` (`YYYY-Www`, `current`, `last`), same as above |
| `monthly` | One month | `--month` (`YYYY-MM`, `current`, `last`), same as above |
| `smart` | Daily + weekly/monthly when due | `--date`, `--repo` |
| `backfill` | Daily summaries for a date range | `--from` (required), `--to`, `--force`, `--repo` |
| `standup` | Standup context from recent journal outcomes | `--since` (`24h`, `3d`, ISO time), `--format json\|text`, `--limit`, `--include-low-signal` |
| `stats` | Journal entry and existing-summary counts | — |

Global options: `-v/--verbose`, `--dry-run` (print without saving).

`--repo` defaults to the workspace's own `origin` repository plus the gptme
project repos.

## Configuration

| Environment variable | Effect |
|----------------------|--------|
| `GPTME_WORKSPACE` | Agent workspace root (default: gptme's workspace detection, then the git root) |
| `AW_SERVER` | ActivityWatch server URL (default `http://localhost:5600`) |
| `GPTME_CC_FALLBACK_CREDS` | Colon-separated Claude credential files to try when the active subscription hits a permanent quota/auth failure |
| `GPTME_CC_CMD_PREFIX` | Command prefix for invoking `claude` (e.g. a credential wrapper) |
| `GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK` | Set to `1` to fall back to `gptme` (via OpenRouter) when all Claude attempts fail (e.g. quota exhausted). Off by default because it sends journal content to an external model |
| `GPTME_ACTIVITY_SUMMARY_GPTME_MODEL` | Model for the gptme fallback |

### Backend trace retention

Backend calls keep their traces; nothing is pruned automatically. The gptme
fallback stores each invocation under
`$XDG_STATE_HOME/gptme-activity-summary/gptme-*` (default
`~/.local/state/gptme-activity-summary/`), including the conversation plus
`stdout.log` and `stderr.log` (with partial output on timeout); the path is
logged before the child starts.

Claude calls require a CLI with `--session-id` support. Each attempt (retries
and alternate credential slots included) clears the parent session identity and
uses a fresh `--session-id`. Ordinary calls keep Claude's native project
trajectories; alternate credential slots run in private `claude-slot-*` state
directories, and only the temporary credential symlink is removed afterwards.

Traces may contain journal content and provider diagnostics — keep their
private permissions when archiving, and don't follow a conversation's
`workspace` symlink when copying them.

## License

MIT
