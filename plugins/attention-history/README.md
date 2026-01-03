# gptme-attention-history

Attention history tracking for gptme - queryable record of context usage.

Enables meta-learning by tracking which files/lessons were active during each session.

## Features

- **Turn recording**: Record context state each turn
- **Session queries**: Get history for specific sessions
- **File analysis**: Track how often files appear in context
- **Co-activation patterns**: Discover which files appear together
- **Keyword effectiveness**: Analyze which keywords trigger activations
- **Underutilization detection**: Find files with poor keyword matching

## Installation

Add to your `gptme.toml`:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins"]
enabled = ["attention_history"]
```

## Usage

### Record context state each turn

```python
record_turn(
    turn_number=5,
    hot_files=["lessons/workflow/git-workflow.md"],
    warm_files=["lessons/tools/shell.md"],
    activated_keywords=["git", "commit"],
    message_preview="How do I commit changes..."
)
```

### Query file history

```python
stats = query_file("lessons/workflow/git-workflow.md")
print(f"HOT: {stats['hot_count']} times")
print(f"Sessions: {stats['sessions_appeared']}")
```

### Find co-activation patterns

```python
pairs = query_coactivation()
# Discover files that often appear together
# Use this to set up co-activation in attention-router
```

### Find underutilized files

```python
underused = find_underutilized()
# Files that are tracked but rarely in HOT tier
# May need better keywords
```

### Get summary statistics

```python
summary = get_summary()
print(f"Avg HOT files per turn: {summary['avg_hot_files_per_turn']}")
```

## API Reference

| Function | Description |
|----------|-------------|
| `record_turn()` | Record context state for a turn |
| `query_session()` | Get history for a session |
| `query_file()` | Get statistics for a specific file |
| `query_coactivation()` | Find files that appear together |
| `query_keyword_effectiveness()` | Analyze keyword activations |
| `get_summary()` | Get overall statistics |
| `find_underutilized()` | Find files with low HOT rate |
| `clear_history()` | Clear history (all or by age) |
| `start_new_session()` | Start new session for tracking |

## Use Cases

### Meta-learning

Correlate context composition with session outcomes:
- Which files were active during successful work?
- Do certain file combinations lead to better results?

### Optimization

Improve keyword matching:
- Find files that are rarely in HOT tier
- Identify keywords that never trigger activations
- Discover natural co-activation patterns

### Debugging

Understand context decisions:
- Why was a file in/out of context?
- What keywords triggered a file?
- When did a file fall to COLD tier?

## Related

- [gptme-attention-router](../attention-router/) - Dynamic context routing
