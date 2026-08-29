# gptme-browser-semantic

Semantic **observe / act / extract** primitives for gptme computer-use,
implemented as **Path A**: a pure-Python layer over gptme's *existing*
Playwright browser tools and its ARIA snapshot. No stagehand dependency, no
new browser-launching code — the module reuses the `gptme.tools.browser`
backend that agents already have.

## Why

The stock `browser` tool is screenshot-and-snapshot based. On a multi-step
page task an agent pays an LLM call on every step:

```
snapshot_page()   # 1 LLM: interpret ARIA
click_element()   # 0
snapshot_page()   # 1 LLM: re-interpret
fill_element()    # 0
snapshot_page()   # 1 LLM: verify
```

The semantic pattern collapses that cost:

```
browser_observe("the submit button")   # 1 LLM-equivalent hop, returns selectors
browser_act(observed[0])               # 0 — reuses the cached selector
browser_act(observed[1], fill="x")     # 0
browser_extract()                      # 0 (raw ARIA)
```

`browser_observe` is the load-bearing primitive: one call produces a ranked
list of reusable Playwright-anchored selectors that subsequent deterministic
actions act on for **zero** extra interpretation cost.

## The three primitives

- **`browser_observe(instruction, *, top_k=5, llm_rerank=False)`**
  Returns a list of `ObserveResult` (description, Playwright selector,
  method, arguments), best match first. The default path is a deterministic
  token-overlap + role-aware scorer over the ARIA snapshot — **no LLM
  round-trip**. `llm_rerank=True` is a Path-B hook (not wired in Path A).

  Selectors: when the snapshot includes `[ref=eN]` (tests / future gptme
  snapshots with `ref=True`), that ref is reused. Otherwise the locator is
  `role={role}[name='{name}']`, which gptme's `click_element` / `fill_element`
  already accept. Non-ref bracket attributes such as `[level=1]` are ignored.

- **`browser_act(action_or_observed, *, method=None, arguments=None,
  retry_on_stale=True)`**
  Two forms:
  1. `browser_act("click the submit button")` — observes internally,
     dispatches the top match.
  2. `browser_act(observed)` — dispatches a previously observed selector.
     Zero interpretation cost.

  **Stale-selector recovery**: when a cached selector no longer resolves
  (the page re-rendered or swapped refs), the dispatch fails and the element
  is re-observed once with the same query and retried on the fresh top match.
  The re-observe is free on the default scoring path, so the retry costs
  nothing against the LLM budget. `retry_on_stale=False` returns the first
  failure immediately.

- **`browser_extract(instruction=None, *, schema=None)`**
  Path A always returns the raw ARIA snapshot (zero-LLM). `schema=` is
  rejected with a Path-B hint — schema-aware typed extraction is not
  silently faked.

## Path A vs Path B

- **Path A (this package)**: implement the semantic interface directly over
  gptme's ARIA snapshot + Playwright. Shippable today, zero new dependencies.
- **Path B**: once `stagehand` exposes a usable local-only Python mode
  (`stagehand.local_browser.launch()`, not yet on PyPI), these same tool
  signatures wrap stagehand and inherit its real semantic model. The
  `ObserveResult` shape is deliberately stagehand-compatible so the swap is
  mechanical.

## Benchmark

`benchmark.py` counts LLM calls across 5 representative page tasks against a
static, deterministic `fixtures/hn.html` (HN clone — no live web, no browser
launch). Recorded result under the conservative production-path proxy
(observe and instructed extract counted as 1 LLM each, as if rerank/typed
extract were on):

| | LLM calls |
|---|---:|
| Raw `browser` path | 11 |
| Path A semantic path | 7 |
| **Reduction** | **−36.4%** |

Path A's default implementation is cheaper than this proxy (0-LLM
token-overlap observe, raw-ARIA extract). The −36% figure is therefore a
lower bound. Run it:

```
make benchmark
```

`tests/test_benchmark.py` pins these totals so the design-doc claim can't
rot silently.

## Tests

```
make test
```

The suite never touches a live browser: `gptme.tools.browser` is stubbed in
`sys.modules`, so observe/act/extract run against a recorded ARIA snapshot
and a recording dispatch layer. Coverage includes ranking, ambiguous labels,
stale selectors, re-observe-on-failure, and the no-ref gptme snapshot shape.

## Usage (inside a gptme agent)

```python
from gptme_browser_semantic import browser_observe, browser_act, browser_extract

obs = browser_observe("the search box")
browser_act(obs[0], method="fill", arguments=["rust async"])
browser_act("click the Search button")
state = browser_extract()
```

Requires the `gptme` browser backend to be installed and a page to be open
(`gptme[browser]` extras + `playwright install chromium`). This package
itself has no runtime dependencies.

## Recovery note

The original Path A prototype lived only in
`/tmp/worktrees/gptme-browser-semantic/` and was lost when that worktree
was removed. This package reconstructs it from the committed design
(`browser-tool-act-observe-extract.md`) and the 5d08 benchmark table.
The 11→7 LLM-call claim is the conservative proxy from that design; it is
pinned here rather than re-invented.
