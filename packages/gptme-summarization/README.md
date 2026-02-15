# gptme-summarization

Journal summarization utilities for gptme agents.

## Features

- **Daily Summarization**: Generate daily summaries from journal entries
- **Weekly Aggregation**: Aggregate daily summaries into weekly summaries
- **Monthly Aggregation**: Aggregate weekly summaries into monthly summaries
- **Claude Code Backend**: High-quality summarization using Claude Code

## Installation

```bash
pip install gptme-summarization
```

## Usage

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

## Integration with gptme

This package is designed to work with gptme agents' journal systems. It can be used as a standalone CLI or integrated into autonomous workflows.

## License

MIT
