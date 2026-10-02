# gptme-consortium — multi-model consensus ("ask several LLMs, then synthesize")

Ask the same question to several LLMs through gptme's provider layer, then
have an arbiter model synthesize a single consensus answer with a confidence
score and its reasoning.

**Status:** experimental. Works, but simple: models are queried sequentially,
and the default model list is pinned in code and ages quickly — pass `models=`
explicitly.

## Why / when to use it

For decisions where one model's blind spots are costly — architecture choices,
reviewing a risky plan, comparing how providers answer a question. You get each
model's raw answer plus a synthesized consensus, so disagreement is visible
rather than hidden.

It uses whatever providers your gptme installation is configured for (API keys
via environment or gptme config); models without working credentials fail
individually and are excluded from the synthesis.

## Install

There is no package entry point; load it as a folder plugin from a
gptme-contrib checkout:

```toml
# gptme.toml (or [plugins] in ~/.config/gptme/config.toml)
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-consortium"]
enabled = ["gptme_consortium"]   # only needed if you use an allowlist
```

This registers a `consortium` tool whose `query_consortium` function is
callable from gptme's Python (ipython) tool.

## Quickstart

```python
result = query_consortium(
    question="For a 3-person team expecting 100K users in 2 years: "
             "modular monolith or microservices?",
    models=[
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-5.1",
        "gemini/gemini-3-pro-preview",
    ],
    arbiter="anthropic/claude-sonnet-4-5",
)
print(result.consensus)
print(f"confidence={result.confidence:.2f} agreement={result.agreement_score:.2f}")
for model, answer in result.responses.items():
    print(model, "→", answer[:200])
```

## API

`query_consortium(question, models=None, arbiter=None, confidence_threshold=0.8, query_delay=0.5) -> ConsortiumResult`

| Argument | Default | Notes |
|----------|---------|-------|
| `models` | `anthropic/claude-opus-4-5`, `openai/gpt-5.1`, `gemini/gemini-3-pro-preview`, `xai/grok-4`, `openrouter/perplexity/sonar-pro` | Any gptme model ID |
| `arbiter` | `anthropic/claude-sonnet-4-5` | Model that writes the synthesis |
| `confidence_threshold` | `0.8` | Passed to the synthesis step; currently informational — it does not reject low-confidence results |
| `query_delay` | `0.5` | Seconds between model queries (rate-limit friendliness) |

`ConsortiumResult` fields: `question`, `consensus`, `confidence`, `responses`
(model → answer or `"Error: ..."`), `synthesis_reasoning`, `models_used`,
`arbiter_model`, `agreement_score`.

How confidence is computed: the arbiter's self-reported 0–1 confidence is
multiplied by the fraction of models that answered successfully and by
`0.5 + 0.5 × agreement_score`. Here `agreement_score` is the average pairwise
word-level Jaccard similarity between the successful answers. Each failed
model call gets up to 3 retries with exponential backoff, and rate-limit
errors wait longer. No exception is raised if the arbiter fails: the call
returns a placeholder consensus with base confidence 0.3. Unparsable arbiter
output is returned as the raw consensus with base confidence 0.5.

## Tests

Unit tests use mocks; tests under `tests/integration/` call real models and
need API keys.
