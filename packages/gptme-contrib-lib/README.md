# gptme-contrib-lib

Shared Python helpers used by other gptme-contrib packages and scripts: the
deterministic merge-policy engine and the AI-review merge-blocking policy.

**Status:** internal helper library (`0.1.0`). No stable API guarantees and no
PyPI release; it exists so contrib packages and scripts can share code.

## Install

As a uv workspace member it is available automatically after
`uv sync --all-packages` in a gptme-contrib checkout. Standalone:

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-contrib-lib"
```

Dependencies: none beyond the standard library (`tomli` on Python 3.10).

## Modules

All modules live under `gptme_contrib_lib` (there is no top-level re-export).

| Module | Contents |
|--------|----------|
| `merge_policy` | Deterministic self-merge policy: loads tiers from the trusted `origin/master` copy of `merge_policy.toml` (`load_policy()` raises `ValueError` outside a checkout with that ref; an explicit path is for offline calibration and tests), classifies changed files and returns an eligibility decision. Stdlib only, so plain scripts (`scripts/github/merge-policy.py`, `self-merge-check.py`) can import it |
| `ai_review_policy` | `blocking_shortfall(rows)` — which AI code-review findings block a merge (P2+ never block; P0/P1 block until disposed, superseded, or both replied to and resolved; unreadable severity fails closed) |

## Backward compatibility

`src/lib` is a symlink to `src/gptme_contrib_lib`, so legacy `from lib.merge_policy
import ...` imports keep working when the `src/` directory is on the import path
(source checkouts). The built wheel only contains `gptme_contrib_lib`.
