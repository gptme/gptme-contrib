# gptme-block-registry

Shared **state-dir block-file contract and registry** for gptme agent arm
dispatch — the credential-survival sublayer, extracted so any forked agent can
read the same block files and make the same dispatch decision without importing
Bob-only modules.

Policy-free by design. This package owns the *wire contract* and the
*evaluation semantics*; the caller owns the policy (which route, which scoped
credential context, which backend rules apply).

## What it provides

| Concern | API |
|---|---|
| Filename derivation | `arm_block_path`, `backend_block_path`, `pool_block_path`, `openrouter_block_path`, `model_safe` |
| Timestamp shape | `format_timestamp`, `parse_until`, `is_canonical_timestamp` |
| Evaluation | `BlockRegistry.check(checks) -> BlockVerdict` |
| Writing | `write_block` (never shortens), `clear_block`, `read_block_until` |
| OpenRouter windows | `limit_window_from_text`, `block_deadline` |

## Quick start

```python
from pathlib import Path

from gptme_block_registry import BlockRegistry

registry = BlockRegistry(Path("state/backend-quota"))

checks = registry.arm_checks("gptme", "glm-5.2")          # crash, daily, quality
checks.append(registry.backend_check("gptme"))            # backend-wide exhaustion
checks.append(registry.openrouter_check(context=None))    # shared OR chain

verdict = registry.check(checks)
if verdict.blocked:
    print(f"blocked: {verdict.reason} until {verdict.until}")
```

Capability tiers: this layer is **Tier 0** — filesystem + stdlib only, no shell,
no service manager, no network. A caller without `gh`/systemd/SSH still gets the
same allow/deny answer; only the *alerting* around it is tiered.

## Semantics that must not drift

- A block is active while its deadline is **in the future**.
- An unreadable or garbled timestamp **fails open** (clear) with a recorded
  warning — a corrupt block file must never brick an arm.
- Writers **never shorten** an existing canonical block; a shorter deadline
  arriving on top of a longer one is ignored. A non-canonical existing value is
  treated as unknown and overwritten.
- Timestamps are fixed-width ISO-8601 UTC (`+00:00`). Lexicographic comparison is
  valid only for that exact shape.
- The `daily` in `openrouter-*-daily-limit-until.txt` is **historical** — the file
  holds an absolute deadline for any limit window (daily/weekly/monthly/credits).

Full contract: see `src/gptme_block_registry/contract.py` docstring.

## Tests

```shell
uv run pytest packages/gptme-block-registry/tests
```
