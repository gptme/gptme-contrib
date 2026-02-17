# gptme-activity-summary

Activity summarization for gptme agents and humans — journals, GitHub, sessions, tweets, email.

## Features

- **Daily Summarization**: Generate daily summaries from journal entries
- **Weekly Aggregation**: Aggregate daily summaries into weekly summaries
- **Monthly Aggregation**: Aggregate weekly summaries into monthly summaries
- **GitHub User Mode**: Summarize any GitHub user's activity (no agent workspace needed)
- **Claude Code Backend**: High-quality summarization using Claude Code
- **Real Data Sources**: GitHub activity, gptme session stats, posted tweets, sent emails
- **Model Usage Tracking**: Per-model token/cost breakdown from conversation logs

## Installation

```bash
pip install gptme-activity-summary
```

## Usage

### Agent Mode (journal-based)

```bash
# Daily summary
summarize daily --date yesterday

# Weekly summary
summarize weekly --week last

# Monthly summary
summarize monthly --month last

# Smart mode (auto-triggers weekly/monthly when appropriate)
summarize smart --date yesterday
```

### GitHub User Mode (human activity)

Summarize any GitHub user's activity without requiring a journal or agent workspace:

```bash
# Weekly GitHub activity summary
summarize github --user ErikBjare --period weekly

# Monthly summary
summarize github --user ErikBjare --period monthly

# Raw data (no LLM summarization)
summarize github --user ErikBjare --period weekly --raw

# Custom date range reference
summarize github --user ErikBjare --period weekly --date 2026-02-10
```

## Integration with gptme

This package is designed to work with gptme agents' journal systems. The `github` command also works standalone for summarizing human GitHub activity — useful for profile READMEs, newsletters, and activity reports.

## License

MIT
