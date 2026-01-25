# gptme-attention-router

Attention-based context routing with HOT/WARM/COLD tiers for gptme.

Implements dynamic context management inspired by [Claude Cognitive](https://github.com/GMaN1911/claude-cognitive).

## Features

- **HOT tier** (score ≥ 0.8): Full file content included
- **WARM tier** (0.25 ≤ score < 0.8): Header/first 30 lines only
- **COLD tier** (score < 0.25): Excluded from context
- **Decay**: Scores decay each turn (default 0.75×)
- **Keyword activation**: Files activate to HOT when keywords match
- **Co-activation**: Related files boost each other's scores
- **Pinning**: Critical files never fall below WARM

## Installation

Add to your `gptme.toml`:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins"]
enabled = ["attention_router"]
```

## Usage

### Register files for tracking

```python
register_file(
    "lessons/workflow/git-workflow.md",
    keywords=["git", "commit", "branch", "push"],
    coactivate_with=["lessons/workflow/git-worktree-workflow.md"],
    pinned=True  # Never falls below WARM
)
```

### Process each turn

```python
# Call this each turn to apply decay and activate by keywords
result = process_turn("How do I commit changes with git?")
# Files with "git" or "commit" keywords activate to HOT
```

### Get context recommendations

```python
rec = get_context_recommendation()
# rec["include_full"] = HOT files to load completely
# rec["include_header"] = WARM files to load headers only
```

### Check current tiers

```python
tiers = get_tiers()
print("HOT:", [f["path"] for f in tiers["HOT"]])
print("WARM:", [f["path"] for f in tiers["WARM"]])
```

## Expected Token Savings

- Full files: 2,000-5,000 tokens each
- Headers only (WARM): 200-500 tokens each
- Expected reduction: 64-95% depending on codebase

## API Reference

| Function | Description |
|----------|-------------|
| `register_file()` | Add a file to attention tracking |
| `unregister_file()` | Remove a file from tracking |
| `process_turn()` | Apply decay and activate by keywords |
| `get_tiers()` | Get current HOT/WARM/COLD assignments |
| `get_score()` | Get attention score for a file |
| `set_score()` | Manually set attention score |
| `get_context_recommendation()` | Get files to include in context |
| `extract_header()` | Get first N lines of a file (WARM tier) |
| `get_status()` | Get router status and statistics |
| `reset_state()` | Reset all attention state |

## Related

- [gptme-attention-history](../gptme-attention-history/) - Track what was in context
- [Claude Cognitive](https://github.com/GMaN1911/claude-cognitive) - Original inspiration
