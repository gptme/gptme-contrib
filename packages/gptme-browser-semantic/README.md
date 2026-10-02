# gptme-browser-semantic

Stagehand-style **observe / act / extract** browser primitives for gptme
computer-use: find elements by natural-language description ("the submit
button") and reuse the resulting Playwright selectors across several actions,
without re-reading the page between steps.

**Status:** experimental (`0.1.0`, alpha). A pure-Python library layered on
gptme's existing Playwright browser tool and its ARIA snapshot — no Stagehand
dependency, no new browser-launching code. It is a Python library, not a
registered gptme tool or plugin: call it from gptme's `ipython` tool or from your
own scripts.

## Why

The stock `browser` tool exposes snapshots and deterministic actions. The
outer agent model normally interprets each fresh snapshot before choosing the
next action:

```text
snapshot_page()   # outer model interprets ARIA
click_element()
snapshot_page()   # outer model re-interprets
fill_element()
snapshot_page()   # outer model verifies
```

The semantic pattern makes selector discovery reusable:

```python
observed = browser_observe("the submit button")
browser_act(observed[0])
browser_extract()
```

`browser_observe` is the load-bearing primitive: one call produces a ranked
list of reusable Playwright-anchored selectors that subsequent deterministic
actions act on with no extra model interpretation.

## Install

Not published to PyPI. Install from the repository subdirectory into the same
environment as gptme:

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-browser-semantic"
```

The package itself has no runtime dependencies, but at runtime it imports
`gptme.tools.browser`, so you need gptme with its browser extra and a Chromium
build for Playwright:

```bash
pip install "gptme[browser]"
playwright install chromium
```

`has_semantic_browser()` returns `True` when that backend is importable.

## Usage

With a page already open in gptme's browser tool:

```python
from gptme_browser_semantic import browser_observe, browser_act, browser_extract

obs = browser_observe("the search box")
browser_act(obs[0], arguments=["rust async"])   # textbox -> fill
browser_act("click the Search button")          # observe + dispatch top match
state = browser_extract()                       # ExtractResult; .data is the ARIA snapshot
```

## The three primitives

- **`browser_observe(instruction, *, top_k=5, llm_rerank=False) -> list[ObserveResult]`**
  Ranked matches, best first. Each `ObserveResult` has `description`,
  `selector`, `method` (`click` | `fill` | `select`, inferred from the ARIA
  role), `arguments`, and the original `instruction`. Scoring is a
  deterministic token-overlap + role-aware scorer over the ARIA snapshot — no
  LLM round-trip. Returns `[]` if nothing matches or no snapshot is available.
  `llm_rerank=True` raises `NotImplementedError` (reserved for a future
  reranker).

  Selectors: when the snapshot line carries `[ref=eN]`, that ref is reused.
  Otherwise the locator is `role={role}[name='{name}']`, which gptme's
  `click_element` / `fill_element` accept. Other bracket attributes such as
  `[level=1]` are ignored.

- **`browser_act(action_or_observed, *, method=None, arguments=None, retry_on_stale=True) -> ActResult`**
  Two forms:
  1. `browser_act("click the submit button")` — observes internally and
     dispatches the top match.
  2. `browser_act(observed)` — dispatches a previously observed selector.

  `method` overrides the inferred action: `click`, `fill`, `select`, `hover`,
  or `press` (`fill`/`select`/`press` need `arguments=[value]`; `press` is
  refused on button/link elements, use `click` instead). A `fill` with no value
  returns a clear failure instead of guessing.

  **Stale-selector recovery**: when a cached selector no longer resolves (the
  page re-rendered or swapped refs), the element is re-observed once with the
  same query and the action retried on the fresh top match. Errors that suggest
  the action already ran (navigation, detached target) are not retried, to
  avoid double-acting. `retry_on_stale=False` returns the first failure
  immediately.

- **`browser_extract(instruction=None, *, schema=None) -> ExtractResult`**
  Always returns the raw ARIA snapshot in `.data`; `instruction` is ignored.
  Passing `schema=` returns `success=False` with a hint — typed extraction is
  not silently faked.

`ActResult` and `ExtractResult` both report `success`, `elapsed_ms`, and
`llm_calls` (always `0` in this implementation).

## Design notes: Path A vs Stagehand

- **Path A (this package)**: implement the semantic interface directly over
  gptme's ARIA snapshot + Playwright. Zero new dependencies.
- **Stagehand**: `stagehand==3.23.0` exposes a local API server and local
  browser, but it is not a mechanical swap: Stagehand requires a CDP
  browser/session boundary, while gptme's current Playwright page is private,
  thread-bound state. Path A stays native until a supported shared-page seam
  and an executed end-to-end benchmark justify the extra server and inner model
  calls. `ObserveResult` mirrors Stagehand's shape to keep that option open.

## Benchmark

`benchmark.py` preserves five representative action sequences and applies a
static counting heuristic. It does not open `fixtures/hn.html`, invoke a
browser or model, or record success. The scenario proxy is:

| | Proxy units |
|---|---:|
| Raw `browser` path | 11 |
| Path A semantic path | 7 |
| **Difference** | **-4** |

This is not a measured LLM, token, latency, or success-rate result. A verdict
requires both paths to execute against the same page while recording outer
agent turns and any inner provider calls separately. Run the proxy with
`make benchmark`. `tests/test_benchmark.py` pins the scenario arithmetic and
its stated limitations so it cannot silently become a performance claim.

## Tests

```bash
make test
```

The suite never touches a live browser: `gptme.tools.browser` is stubbed in
`sys.modules`, so observe/act/extract run against a recorded ARIA snapshot and
a recording dispatch layer. Coverage includes ranking, ambiguous labels, stale
selectors, re-observe-on-failure, and the no-ref gptme snapshot shape.

## See also

- gptme's built-in [`browser` tool](https://gptme.org/docs/tools.html) — the
  backend this package drives.
- [gptme-contrib packages index](../README.md)
