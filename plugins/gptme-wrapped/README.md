# gptme-wrapped

"Spotify Wrapped" for your [gptme](https://gptme.org) usage: a yearly report of conversations, tokens, LLM costs, favourite models, cache efficiency and when you use gptme most, built from your local conversation logs.

**Status:** experimental. Read-only; it never modifies your logs.

## What it reports

- Conversations and messages per year
- Input/output tokens, cache reads/writes and cache hit rate, with a rough savings estimate
- Total cost, plus breakdowns by model and by month
- Top models by usage
- Peak hour and weekday, and a GitHub-style activity heatmap

Data comes from the `conversation.jsonl` files in gptme's logs directory (by default `~/.local/share/gptme/logs/`). Token and cost figures depend on per-message metadata, which older gptme versions didn't record. Conversations without it still count towards conversation and message totals, and the report shows how many conversations had metadata.

## Standalone CLI

No gptme session needed. Run it from the gptme-contrib workspace:

```sh
cd /path/to/gptme-contrib
uv run --package gptme-wrapped python -m gptme_wrapped                    # this year's report
uv run --package gptme-wrapped python -m gptme_wrapped report 2025        # a specific year
uv run --package gptme-wrapped python -m gptme_wrapped heatmap            # activity heatmap
uv run --package gptme-wrapped python -m gptme_wrapped stats              # raw stats as JSON
uv run --package gptme-wrapped python -m gptme_wrapped export -f html > wrapped.html   # json (default), csv or html
```

Or run it without cloning:

```sh
uv run --with "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-wrapped" \
  python -m gptme_wrapped
```

Every subcommand takes an optional `YEAR` argument and defaults to the current year.

## Use inside gptme

Point gptme at the parent `plugins/` directory and enable the plugin by its directory name, in `gptme.toml` or `~/.config/gptme/config.toml`:

```toml
[plugins]
paths = ["/path/to/gptme-contrib/plugins"]
enabled = ["gptme-wrapped"]
```

This adds a `wrapped` tool whose functions the agent calls from Python blocks, so you can just ask "show me my gptme wrapped":

```python
print(wrapped_report())          # formatted report, current year
wrapped_stats(2025)              # dict of raw statistics
print(wrapped_heatmap())         # activity heatmap
print(wrapped_export(format="csv"))   # "json", "csv" or "html"
```

All four functions take an optional `year`.

## Example output

```text
🎁 gptme Wrapped 2025 🎁
========================================

📊 Your Year in Numbers:
  • 847 conversations
  • 12,543 messages
  • 45.2M input tokens
  • 2.1M output tokens
  • $127.34 total cost

🤖 Top Models:
  1. claude-sonnet-4-20250514 (67%)
  2. gpt-4 (21%)
  3. claude-3-opus (8%)

⏰ Peak Usage:
  • Most active hour: 14:00-15:00
  • Most active day: Wednesday

💾 Cache Efficiency:
  • Cache hit rate: 73%
  • Cached tokens: 33.0M
  • Est. savings: $89.50

📅 Monthly Breakdown:
  2025-01: $8.23    ████
  2025-02: $12.45   ██████
  ...
```

## Development

```sh
# from the gptme-contrib repo root
uv run pytest plugins/gptme-wrapped/tests
```

## Related

- [gptme-wrapped skill](../../skills/gptme-wrapped/SKILL.md): the conversation storage format and further analysis ideas
- [gptme-usage](../../packages/gptme-usage/): cross-backend usage, cost and quota tracking
