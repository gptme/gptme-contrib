# gptme-block-registry

Shared file-based "is this model/backend blocked right now?" contract for
autonomous agents that fail over between LLM backends — quota exhaustion,
crash loops, OpenRouter key limits — plus a per-credential circuit breaker and
a config-driven fallback arm for when credentials die.

**Status:** alpha (`0.1.0`). Extracted from a production agent's dispatch layer
so forked agents can read and write the same block files and reach the same
dispatch decision without importing that agent's internal modules.

Policy-free by design. This package owns the *wire contract* (filenames,
timestamp shape, fail-open reads, never-shorten writes) and the *evaluation
semantics*; the caller owns the policy (which route, which scoped credential
context, which backend rules apply).

**Tier 0:** filesystem + standard library only (plus `tomli` on Python 3.10) —
no shell, no service manager, no network. A caller without `gh`, systemd or
SSH still gets the same allow/deny answer.

## Install

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-block-registry"
# or, from a gptme-contrib checkout:
uv pip install -e packages/gptme-block-registry
```

## What it provides

| Concern | API |
|---|---|
| Filename derivation | `arm_block_path`, `backend_block_path`, `pool_block_path`, `openrouter_block_path`, `model_safe`, `ARM_BLOCK_KINDS` |
| Timestamp shape | `format_timestamp`, `parse_until`, `is_canonical_timestamp` |
| Evaluation | `BlockRegistry.check(checks) -> BlockVerdict` |
| Writing | `write_block` (never shortens, atomic, locked), `clear_block`, `read_block_until` |
| OpenRouter limit windows | `limit_window_from_text`, `block_deadline` |
| Per-slot circuit breaker | `decide_respawn`, `AUTH_DEATH` / `PRODUCTIVE` / `NEUTRAL`, `CircuitState` |
| Fallback arm | `load_fallback_config`, `decide_fallback`, `FallbackArm`, `FallbackConfig`, `FallbackDecision`, `LIGHTS_ON_SCOPE`, `LIGHTS_ON_CATEGORIES` |

## Quick start: check before dispatch

```python
from pathlib import Path

from gptme_block_registry import BlockRegistry

registry = BlockRegistry(Path("state/backend-quota"))

checks = registry.arm_checks("gptme", "glm-5.2")          # crash, daily crash, quality
checks.append(registry.backend_check("gptme"))            # backend-wide exhaustion
checks.append(registry.openrouter_check(context=None))    # bare SHARED OpenRouter key

verdict = registry.check(checks)
if verdict.blocked:
    print(f"blocked: {verdict.reason} until {verdict.until}")
for warning in verdict.warnings:   # fail-open reads are recorded, not hidden
    print(warning)
```

Checks are evaluated in order and the first active block wins. Use
`registry.pool_check(pool)` for routes that share a credential pool.

## Writing blocks

```python
from gptme_block_registry import block_deadline, limit_window_from_text, write_block

window = limit_window_from_text(error_body)   # "daily" | "weekly" | "monthly" | "credits" | None
if window:
    write_block(registry.openrouter_check().path, block_deadline(window))
```

`block_deadline` maps `daily` to the next UTC midnight, `weekly`/`monthly` to
now + 24 h (those windows are relative to the key, so there is no calendar
boundary to target), and `credits` to now + 4 h.

## Block files

All files live in one state directory chosen by the caller (conventionally
`<workspace>/state/backend-quota/`):

| Family | Filename |
|--------|----------|
| Crash loop | `{backend}-{model_safe}-crash-loop-until.txt` |
| Daily crash loop | `{backend}-{model_safe}-daily-crash-loop-until.txt` |
| Quality | `{backend}-{model_safe}-quality-blocked-until.txt` |
| Backend-level | `{backend}-blocked-until.txt` |
| Pool-level | `pool-{pool}-blocked-until.txt` |
| OpenRouter limit (scoped) | `openrouter-{context}-daily-limit-until.txt` |
| OpenRouter limit (SHARED key) | `openrouter-daily-limit-until.txt` |

Each OpenRouter file covers exactly one key. The unscoped file is the bare
SHARED key (`OPENROUTER_API_KEY`), not a whole dispatch chain: if a chain
exports a dedicated key first and falls back to SHARED, treat it as blocked
only when both files are active.

`model_safe(model)` replaces `/` and spaces with `-`. Canonicalize model
aliases before deriving a filename so an alias and its full name hit the same
file.

### Semantics that must not drift

- A block is active while its deadline is **in the future**.
- An unreadable or garbled timestamp **fails open** (clear) with a recorded
  warning — a corrupt block file must never brick an arm.
- Writers **never shorten** an existing canonical block; a shorter deadline
  arriving on top of a longer one is ignored. A non-canonical existing value is
  treated as unknown and overwritten.
- Timestamps are fixed-width ISO-8601 UTC (`+00:00`). Lexicographic comparison
  is valid only for that exact shape.
- The `daily` in `openrouter-*-daily-limit-until.txt` is **historical** — the
  file holds an absolute deadline for any limit window
  (daily/weekly/monthly/credits).

Full contract: the `src/gptme_block_registry/contract.py` module docstring.

## Credential-death circuit breaker and fallback

When a worker keeps dying on auth errors (e.g. a rotated OAuth token), respawning
it just burns attempts. `decide_respawn` keeps per-slot breaker state in a JSON
file (guarded by an `fcntl` lock, so concurrent spawn loops are safe) and tells
the spawn loop whether to suppress the next spawn. `decide_fallback` then decides
whether to run a cheaper configured arm instead:

```python
from pathlib import Path

from gptme_block_registry import (
    AUTH_DEATH, decide_fallback, decide_respawn, load_fallback_config,
)

suppress, state = decide_respawn(
    state_file=Path("state/slot-circuit-breaker.json"),
    slot="arm-a",
    verdict=AUTH_DEATH,   # previous attempt's outcome: AUTH_DEATH, PRODUCTIVE or NEUTRAL
)
decision = decide_fallback(
    suppress=suppress,
    breaker_state=state,
    config=load_fallback_config(Path("fallback.toml")),
)
if decision:
    arm = decision.arm   # spawn arm.backend / arm.model; stamp arm.label on the session record
```

- The breaker opens after `threshold` consecutive auth deaths (default 3) and
  allows a half-open probe after `cooldown` seconds (default 300).
- Pass `NEUTRAL` for new work with no prior attempt — it checks state without
  resetting the failure count.
- The fallback fires only when the breaker is `OPEN` (not during a half-open
  probe) and an arm is configured.

Fallback config (TOML; a missing file or section means "no fallback"):

```toml
[on_auth_death]
backend = "gptme"
model   = "openrouter/deepseek/deepseek-chat-v3-0324:free"
scope   = "lights-on"
label   = "fallback:auth-death"
note    = "Cheap OpenRouter arm while the primary credential is down"
```

`scope = "lights-on"` restricts the arm to `LIGHTS_ON_CATEGORIES` (standup,
monitoring, escalation, cleanup, small code fixes, infrastructure); check with
`arm.is_category_allowed(category)`. The package never spawns anything itself —
turning a `FallbackDecision` into a command is up to the caller.

## Related

- [credential-slots](../credential-slots/README.md) — safe switching between
  OAuth credential files.
- [gptme-subscription](../gptme-subscription/README.md) — quota-aware
  subscription routing.

## Tests

```shell
uv run pytest packages/gptme-block-registry/tests
```
