# gptme-usage

Cost estimation and model/quota registry for agents that run on several LLM
backends (Claude Code, Codex, gptme via OpenRouter or local models): turn a
session's token counts — or just its duration — into an estimated USD cost, and
map each model to the quota pool it draws from.

**Status:** alpha, library only (no CLI yet). The package ships the generic
math and an empty registry; each agent supplies its own prices, throughput and
routes in a TOML file.

## When to use it

- You log agent sessions and want comparable cost numbers across backends,
  including cache-aware pricing (cache reads/writes priced at provider
  multipliers rather than full input price).
- Some sessions lack token counts and you want a duration-based estimate from
  empirical tokens-per-second.
- You select harness/model per run and need to know which quota pool a model
  uses (`openrouter`, `chatgpt`, `local`, Claude subscription, …).
- You dispatch Claude Code and want an explicit, versioned model ID instead of a
  floating alias like `opus`.

Related packages:

- [gptme-sessions](../gptme-sessions/README.md) — session records and analytics;
  uses `gptme-usage` for token-based cost estimates via its optional `cost`
  extra.
- [gptme-subscription](../gptme-subscription/README.md) — credential-slot
  rotation. Layering rule: **`gptme_usage` never imports
  `gptme_subscription`** — usage/capacity is a separate concern from flipping
  credentials, and spans backends that have no credential slot at all.

## Install

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-usage"
```

Or, from a gptme-contrib checkout: `uv sync --package gptme-usage`.

The only runtime dependency is `tomli` on Python 3.10.

## Configure

Copy [`harness-quota.example.toml`](./harness-quota.example.toml) to
`~/.config/gptme/harness-quota.toml` (or gptme's configured config dir) and
replace the placeholder values:

```toml
claude_plan_tier = "max-5x"      # optional

[prices.claude-code]             # USD per 1M tokens: [input, output]
opus   = [5.0, 25.0]
sonnet = [3.0, 15.0]

[cache_read_prices.claude-code]  # optional absolute USD per 1M cached tokens
opus = 0.50                     # overrides the provider multiplier; 0 means free

[tps.claude-code]                # tokens/second, for duration-based estimates
opus = 18000

[quota_sources]                  # gptme model -> "openrouter" | "chatgpt" | "local"
"example-openrouter-model" = "openrouter"

[model_routes]                   # short name -> provider-qualified gptme model
"example-openrouter-model" = "openrouter/vendor/example-model@vendor"

[openrouter_key_contexts]
default = "autonomous"
```

A missing or unreadable file (or Python 3.10 without `tomli`) yields an empty
config; cost functions then return `None` instead of guessing. A non-empty table in your config *replaces* the
module default table rather than merging with it. Use
`merge_with_module_defaults()` if you want merge semantics.

Optional `[cache_read_prices.<harness>]` entries use the same canonical model
keys as `[prices.<harness>]`. They override provider cache-read multipliers,
including with an explicit zero. Omitted entries retain the provider heuristic
(or regular-input pricing where no heuristic is known); negative/non-finite
rates are ignored. Cache creation pricing and each harness's token-counter
conventions are unchanged. These are API-equivalent estimates, not invoices or
subscription marginal costs; a session's reported cost remains authoritative.

## Quickstart

```python
from gptme_usage import load_quota_config, estimate_session_cost, estimate_tokens_from_duration

cfg = load_quota_config()  # ~/.config/gptme/harness-quota.toml, or empty

cost = estimate_session_cost(
    "claude-code", "opus",
    input_tokens=20_000, output_tokens=5_000, cache_read_tokens=1_000_000,
    config=cfg,
)  # float USD, or None if the model has no configured price

tokens = estimate_tokens_from_duration("claude-code", "opus", 600, config=cfg)
```

## API

Exported from `gptme_usage`:

| Name | Purpose |
|---|---|
| `load_quota_config(path=None)` | Load `HarnessQuotaConfig` from TOML (empty config on any failure) |
| `HarnessQuotaConfig` | Dataclass: `price_table`, `tps_table`, `quota_sources`, `model_routes`, `openrouter_key_contexts`, `claude_plan_tier` |
| `estimate_session_cost(harness, model, *, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, token_count, config)` | Cache-aware USD estimate |
| `estimate_tokens_from_duration(harness, model, duration_seconds, *, config)` | Tokens from duration × configured TPS |
| `pricing_key_for_model(harness, model, config=None)` | Normalize model names (versioned Claude IDs, provider-qualified gptme routes, `@provider` pins) to a pricing-table key |
| `merge_with_module_defaults(config)` | Overlay your config on the module defaults |

More helpers live in `gptme_usage.harness_models`, including
`quota_pool_label()`, `resolve_gptme_model()`, `openrouter_models()`,
`local_models()`, and the Claude Code model-ID helpers
`resolve_cc_version()` / `cc_dispatch_model_id()` (e.g.
`cc_dispatch_model_id("opus-5-5") -> "claude-opus-5-5"`; floating aliases map
to a pinned, validated version).

## Roadmap

- Move per-backend quota checks behind a `gptme-usage check <backend>` console
  entry point (today they live in agent workspaces).
