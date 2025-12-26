# gptme-wrapped

Year-end analytics for your gptme usage - inspired by Spotify Wrapped.

## Features

- **Token usage tracking**: Input/output tokens, cache hits
- **Cost analysis**: Total spend, by model, by month
- **Model preferences**: Most used models
- **Usage patterns**: Peak hours, active days
- **Cache efficiency**: Prompt caching effectiveness

## Installation

```bash
pip install -e plugins/wrapped
```

## Usage

Once installed, the wrapped tool is available in gptme:

```python
# Get your Wrapped report
print(wrapped_report())

# Get detailed stats
stats = wrapped_stats(2025)

# Export to JSON/CSV/HTML
print(wrapped_export(format='json'))
```

## Example Output

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

## Note on Data Availability

Token and cost metadata tracking is relatively recent in gptme. For the best analytics:
- 2025 data may be partial (depends on when you started using recent versions)
- 2026+ will have complete metadata from the start

Historical conversations without metadata are still counted but won't contribute to token/cost totals.

## Related

- [gptme-wrapped skill](../../skills/gptme-wrapped/SKILL.md) - Understanding the storage format
- [gptme documentation](https://gptme.org/docs/)
