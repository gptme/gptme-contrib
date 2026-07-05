# gptme-attention-tracker

Attention tracking and routing plugin for gptme. Combines two complementary tools for context optimization:

1. **attention_history**: Queryable record of what was in context during each session
2. **attention_router**: Dynamic context loading with HOT/WARM/COLD tiers

## Installation

```bash
uv pip install -e plugins/gptme-attention-tracker
```

## Configuration

Add to your `gptme.toml`:

```toml
[plugins]
paths = ["plugins/gptme-attention-tracker/src"]
enabled = ["gptme_attention_tracker"]
```

## Tools Overview

### Attention History

Track and analyze what files/lessons were in context. Enables:
- **Meta-learning**: Correlate context with outcomes
- **Pattern analysis**: Find files that activate together (co-activation)
- **Debugging**: Understand why files were/weren't in context
- **Optimization**: Identify underutilized files with poor keyword matching

**Key Functions**:
- `record_turn()` - Record context state for a turn
- `query_file()` - Query history for a specific file
- `query_coactivation()` - Find files that frequently appear together
- `find_underutilized()` - Find files rarely in HOT tier

### Attention Router

Manage dynamic context loading based on attention scores:
- **HOT tier** (score ≥ 0.8): Full file content included
- **WARM tier** (0.25 ≤ score < 0.8): Header/summary only
- **COLD tier** (score < 0.25): Excluded from context

**Features**:
- **Decay**: Scores decay each turn when not activated
- **Keywords**: Files activate to HOT tier when keywords match
- **Co-activation**: Related files boost each other's scores
- **Pinning**: Critical files never fall below WARM tier
- **Cache-aware batching**: Updates sync with cache invalidation

**Key Functions**:
- `register_file()` - Register a file for tracking with keywords
- `process_turn()` - Process a turn to update attention scores
- `get_tiers()` - Get current HOT/WARM/COLD assignments
- `get_context_recommendation()` - Get files to include in context

## Usage Example

```python
from gptme_attention_tracker.tools.attention_router import (
    register_file, process_turn, get_context_recommendation
)
from gptme_attention_tracker.tools.attention_history import (
    record_turn, query_coactivation
)

# Register files for attention tracking
register_file(
    "lessons/workflow/git-workflow.md",
    keywords=["git", "commit", "branch"],
    pinned=True
)

# Process each turn
result = process_turn("How do I commit changes with git?")

# Get context recommendations
rec = get_context_recommendation()
print("HOT files:", rec["include_full"])
print("WARM files:", rec["include_header"])

# Record turn for history
record_turn(
    turn_number=1,
    hot_files=rec["include_full"],
    warm_files=rec["include_header"],
    activated_keywords=["git", "commit"]
)

# Analyze co-activation patterns
pairs = query_coactivation()
for p in pairs[:5]:
    print(f"{p['file1']} <-> {p['file2']}: {p['count']} times")
```

## State Files

The plugin stores state in `.gptme/`:
- `.gptme/attention_state.json` - Current attention scores and configuration
- `.gptme/attention_history.jsonl` - Historical record of context usage

## Token Savings

Expected savings with attention routing:
- Full files: 2-5k tokens each
- Headers only: 200-500 tokens each
- **64-95% token reduction** depending on codebase

## Related

- [gptme documentation](https://gptme.org)
