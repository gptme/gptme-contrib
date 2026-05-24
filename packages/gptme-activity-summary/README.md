# gptme-activity-summary

Activity summarization for gptme agents and humans — journals, GitHub, sessions, tweets, email, and local time tracking.

> **Supersedes [whatdidyougetdone](https://github.com/TimeToBuildBob/whatdidyougetdone)** — the human work-report use case lives here now, with ActivityWatch as the primary time-tracking source and GitHub as an optional overlay.

## Features

- **Human Mode**: ActivityWatch time tracking as the primary source, GitHub as optional overlay — no agent workspace needed
- **Agent Mode**: Journal-based summaries with GitHub, session stats, tweets, and email
- **Daily/Weekly/Monthly**: All three periods work in both modes
- **LLM Synthesis**: Claude Code backend for narrative summaries (or `--raw` for plain data)
- **Real Data Sources**: ActivityWatch, GitHub activity, gptme session stats, posted tweets, sent emails
- **Model Usage Tracking**: Per-model token/cost breakdown from agent session logs

## Installation

```bash
pip install gptme-activity-summary
```

## Usage

### Human Mode (ActivityWatch + optional GitHub)

Generate work reports from local time tracking with optional GitHub overlay.
No agent workspace or journal required — works for any human user.

```bash
# Daily report (ActivityWatch only)
gptme-activity-summary daily --mode human --date yesterday

# Daily with GitHub activity overlaid
gptme-activity-summary daily --mode human --date yesterday --github-user ErikBjare

# Weekly report (AW + GitHub)
gptme-activity-summary weekly --mode human --week last --github-user ErikBjare

# Monthly report
gptme-activity-summary monthly --mode human --month last --github-user ErikBjare

# Raw data — skip LLM summarization, print source data only
gptme-activity-summary weekly --mode human --week last --github-user ErikBjare --raw
```

**ActivityWatch is the primary data source.** Install and run [ActivityWatch](https://activitywatch.net) on your machine for time-tracking data. If AW is not running, AW data is skipped gracefully and GitHub-only reports still work.

### Agent Mode (journal-based)

```bash
# Daily summary from journal entries
gptme-activity-summary daily --date yesterday

# Weekly summary
gptme-activity-summary weekly --week last

# Monthly summary
gptme-activity-summary monthly --month last

# Smart mode — auto-triggers weekly/monthly when appropriate
gptme-activity-summary smart --date yesterday
```

## Requirements

- **Human mode**: [ActivityWatch](https://activitywatch.net) (optional, for time tracking) + `gh` CLI (optional, for GitHub data)
- **Agent mode**: gptme agent workspace with a `journal/` directory

## License

MIT
