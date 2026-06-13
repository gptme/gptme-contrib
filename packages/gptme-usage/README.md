# gptme-usage

Cross-backend **usage / cost / quota** surface for gptme agents.

This package owns the *usage and capacity* concern: the model registry, cost
math, and per-agent quota configuration that inform harness/model selection for
autonomous runs (and, downstream, subscription pressure scoring). It is
deliberately **separate** from `gptme-subscription`, which owns credential-slot
rotation — a different concern (see "Why a separate package" below).

## What's here

- `harness_models.py` — model registry, cost estimation
  (`estimate_session_cost`, `estimate_tokens_from_duration`), cache pricing
  multipliers, Agent SDK credit facts, and the per-agent quota config schema
  (`HarnessQuotaConfig` + `load_quota_config()`).

## Per-agent config

Agent-specific data (price tables, TPS estimates, model routes, quota sources,
plan tier) lives in `~/.config/gptme/harness-quota.toml`, loaded via
`load_quota_config()`. The package ships **no agent's data** — an unconfigured
agent gets an empty config and the generic cost math degrades gracefully.

```python
from gptme_usage import load_quota_config, estimate_session_cost

cfg = load_quota_config()  # ~/.config/gptme/harness-quota.toml (or empty)
cost = estimate_session_cost("claude-code", "opus", cache_read_tokens=1_000_000, config=cfg)
```

TOML schema: see the `load_quota_config` docstring in `harness_models.py`.

## Why a separate package

`harness_models` / quota checking spans backends with no credential slot at all
(OpenRouter API key, local LM Studio) and never flips a credential symlink. It
is a *usage/capacity* concern, not a *subscription/slot* concern. Keeping it out
of `gptme-subscription` lets both the subscription manager and the autonomous
harness selector depend on usage without dragging in each other.

Layering invariant: **`gptme_usage` must not import from `gptme_subscription`.**
A top-level quota CLI may compose both, but the libraries stay decoupled.

Design: `ErikBjare/bob knowledge/technical-designs/gptme-usage-package-split.md`.

## Roadmap

- **Done**: scaffold + move `harness_models` out of `gptme-subscription`.
- **Done**: config-driven data. The module ships EMPTY tables; `check-quota.py`
  loads `load_quota_config()` once and threads it through every call, so each
  agent's `harness-quota.toml` drives cost/availability. See
  `harness-quota.example.toml` for the schema.
- **Next**: move `check-quota.py` + the `check-*-usage` scrapers in behind a
  `gptme-usage check <backend>` console entry point.
