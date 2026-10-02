# gptme-attention-tracker — HOT/WARM/COLD context tiers and context-usage history

Decide which workspace files deserve a place in an agent's context window:
files gain attention when their keywords come up, decay when unused, and are
sorted into HOT (include fully), WARM (include header only) and COLD (leave
out) tiers. A companion history log records what was in context each turn, so
you can find files that are never used or always co-occur.

**Status:** experimental. The tools are advisory: they compute tiers and
recommendations, but do not themselves rewrite gptme's context. You (or your
agent's context script) decide what to include.

## Why / when to use it

Agents with large always-included context (lessons, knowledge docs, task
files) pay for every token on every turn. This plugin is a building block for
trimming that:

- **attention_router** — keyword-activated scores with decay, co-activation
  and pinning, producing a "what to include" recommendation.
- **attention_history** — a JSONL record of HOT/WARM/COLD files per turn for
  meta-learning: co-activation pairs, keyword effectiveness, underutilized
  files.

Related: [gptme-ace](../gptme-ace/README.md) (hybrid lesson retrieval),
[gptme-retrieval](../gptme-retrieval/README.md), and
[gptme-tooloutput-trimmer](../gptme-tooloutput-trimmer/) for trimming tool
output instead of static context.

## Install

There is no package entry point; load it as a folder plugin from a
gptme-contrib checkout:

```toml
# gptme.toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-attention-tracker"]
enabled = ["gptme_attention_tracker"]   # only needed if you use an allowlist
```

This registers two tools, `attention_router` and `attention_history`, whose
functions are callable from gptme's Python (ipython) tool. Inside the
gptme-contrib uv workspace the package is also importable as
`gptme_attention_tracker`.

## Quickstart

```python
from gptme_attention_tracker.tools.attention_router import (
    register_file, process_turn, get_context_recommendation,
)
from gptme_attention_tracker.tools.attention_history import (
    record_turn, query_coactivation,
)

# Track a file; keyword hits push it to HOT. Pinned files never drop below WARM.
register_file("lessons/workflow/git-workflow.md",
              keywords=["git", "commit", "branch"], pinned=True)

# Updates are batched by default (flushed after 10 turns or on cache
# invalidation); apply_now=True applies them immediately.
process_turn("How do I commit changes with git?", apply_now=True)

rec = get_context_recommendation(max_hot=10, max_warm=20)
print("HOT:", rec["include_full"])     # include full content
print("WARM:", rec["include_header"])  # include header only

record_turn(turn_number=1,
            hot_files=rec["include_full"],
            warm_files=rec["include_header"],
            activated_keywords=["git", "commit"])

for p in query_coactivation()[:5]:
    print(f"{p['file1']} <-> {p['file2']}: {p['count']}")
```

## How scoring works

| Tier | Score | Recommendation |
|------|-------|----------------|
| HOT  | ≥ 0.8 | include full content |
| WARM | 0.25 – 0.8 | include header (first 30 lines, see `extract_header`) |
| COLD | < 0.25 | exclude |

- New files start at 0.5 (`initial_score`); every processed turn multiplies
  each score by its decay rate (default 0.75, per-file override via
  `decay_rate`).
- A keyword match resets a file's score to 1.0 (HOT); files listed in its
  `coactivate_with` get +0.35.
- Pending updates are flushed on gptme's `CACHE_INVALIDATED` hook (when
  available) so tier changes line up with prompt-cache invalidation instead of
  breaking the cache every turn.

## API

**attention_router**: `register_file`, `unregister_file`, `process_turn`,
`flush_pending_updates`, `get_tiers`, `get_score`, `set_score`,
`get_context_recommendation`, `extract_header`, `get_status`, `reset_state`.

**attention_history**: `record_turn`, `start_new_session`, `query_session`,
`query_file`, `query_coactivation`, `query_keyword_effectiveness`,
`get_summary`, `find_underutilized`, `clear_history`.

## State files

Both paths are relative to the current working directory:

- `.gptme/attention_state.json` — current scores and per-file configuration
- `.gptme/attention_history.jsonl` — per-turn context history
