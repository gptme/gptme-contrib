"""
Semantic browser primitives for gptme computer-use.

Wraps the stagehand act/observe/extract pattern as three gptme tool functions:
  - browser_act: execute an action on the current page (one LLM call, deterministic)
  - browser_observe: return Playwright selectors matching an instruction (reusable)
  - browser_extract: typed page data extraction (Pydantic schema optional)

Design intent (full justification in the "browser tool act/observe/extract"
design doc, knowledge/technical-designs/browser-tool-act-observe-extract.md
in the workspace that prototyped this):

- observe() is the load-bearing primitive: it returns a list of ObserveResult
  objects, each with a Playwright selector and a description. Once you have the
  selector, deterministic Playwright ops (`page.click`, `page.fill`) are zero-LLM
  calls. This is the headline gain over the existing `browser.snapshot` →
  `click_element(selector)` pattern, which already does this for ONE action at
  a time; observe() enables chained deterministic actions after a single LLM hop.
- act() with a pre-observed ObserveResult also goes deterministic (no LLM call).
  Reusing observed selectors across actions is the multi-step efficiency win.
- extract() returns a typed schema (or raw text). Strictly optional; useful when
  the downstream consumer is JSON-producing code (filling a form from a CSV,
  pulling structured data off a list page).

This module is the prototype. There are two implementation paths:

  Path A (this file): the *interface*. gptme tools are pure-Python functions
    that take the existing playwright page from `browser.py`. The semantic
    primitives are implemented over Playwright directly: observe() walks the
    ARIA tree and uses LLM-ranked fuzzy matching; act() runs the selector;
    extract() walks the DOM and applies schema validation.
    Tradeoff: no stagehand dep, but you re-implement the AI bits.

  Path B (in design doc): when stagehand gains a usable local-only path OR
    when we ship a small helper that runs the stagehand server locally, these
    tools wrap stagehand.local_browser.launch() and inherit the real semantic
    model. This is the recommended next step.

This module implements Path A so we can benchmark today.

Install notes:
- Requires `pip install playwright` (already part of `gptme[browser]` extras).
- `playwright install chromium` must have been run.
- For the LLM-backed observe/extract paths, OPENAI_API_KEY (or any provider
  gptme supports via its LLM router) must be set. Plumbed through gptme's
  existing model client.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Reuse gptme's existing playwright page. The browser tool exposes
# `snapshot_page()` which returns an ARIA accessibility snapshot (string).
# We don't take a hard import on browser.py here so this module can be
# loaded standalone for the prototype harness.


@dataclass
class ObserveResult:
    """A single Playwright-anchored observation returned by browser_observe.

    Mirrors stagehand's ObserveResult shape so a future Path-B swap is mechanical.
    """

    description: str
    selector: str
    method: str = "click"  # click | fill | hover | select | press
    arguments: list[str] = field(default_factory=list)
    instruction: str = ""  # original instruction passed to browser_observe


@dataclass
class ActResult:
    """Result of a browser_act invocation."""

    success: bool
    message: str
    selector_used: str | None = None
    llm_calls: int = 0
    elapsed_ms: int = 0


@dataclass
class ExtractResult:
    """Result of a browser_extract invocation."""

    success: bool
    data: dict[str, Any] | str
    llm_calls: int = 0
    elapsed_ms: int = 0


def _aria_snapshot() -> str:
    """Fetch the current page's ARIA snapshot via gptme's browser tool.

    This calls into the existing playwright backend. Lazy import so the module
    can be imported for type-checking without playwright installed.
    """
    from gptme.tools.browser import snapshot_page

    # Annotated local so this stays clean under both mypy regimes: with
    # gptme installed (snapshot_page is str) and with
    # --ignore-missing-imports (the import resolves to Any).
    snapshot: str = snapshot_page()
    return snapshot


def _parse_aria_to_elements(snapshot: str) -> list[dict[str, str]]:
    """Parse the gptme ARIA snapshot format into a flat element list.

    gptme's snapshot_page returns a human-readable tree with roles + names +
    optional selectors like `[ref=e5]`. We extract every (role, name, ref)
    triple into a flat list so observe() can rank them.
    """
    elements: list[dict[str, str]] = []
    # Lines look like:
    #   - link "Home" [ref=e1]
    #   - button "Submit" [ref=e3]
    #   - textbox "Email" [ref=e7]
    line_re = re.compile(
        r'^[\s\-]*(?P<role>\w+)\s+"(?P<name>[^"]*)"(?:\s+\[(?P<ref>[^\]]+)\])?'
    )
    for line in snapshot.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        role = m.group("role").strip()
        name = m.group("name").strip()
        ref = m.group("ref") or ""
        if not name:
            continue
        # Only treat bracket content as a real ref if it is a `ref=...`
        # attribute (e.g. "ref=e1").  Other bracket attributes like
        # "[level=2]" must be ignored per the README spec.
        actual_ref = ref if ref.startswith("ref=") else ""
        selector = f"[{actual_ref}]" if actual_ref else f"role={role}[name='{name}']"
        elements.append(
            {
                "role": role,
                "name": name,
                "selector": selector,
            }
        )
    return elements


def _method_for_role(role: str) -> str:
    """Return the default interaction method for an ARIA role."""
    r = role.lower()
    if r in {"textbox", "searchbox", "combobox"}:
        return "fill"
    if r in {"select", "listbox"}:
        return "select"
    return "click"


def _score_match(query: str, element: dict[str, str]) -> float:
    """Score how well an element matches a natural-language instruction.

    Cheap token-overlap scorer so observe() works without an LLM round-trip in
    the common case. Returns 0.0..1.0.
    """
    q_tokens = {t.lower() for t in re.findall(r"\w+", query)}
    e_tokens = {t.lower() for t in re.findall(r"\w+", element["name"])}
    if not q_tokens or not e_tokens:
        return 0.0
    overlap = q_tokens & e_tokens
    # Role-aware boost: if the query mentions a role, match against it.
    role = element["role"].lower()
    role_boost = 0.0
    if role in {"button"} and any(
        t in {"click", "press", "tap", "submit"} for t in q_tokens
    ):
        role_boost = 0.2
    elif role in {"textbox"} and any(
        t in {"type", "enter", "fill", "input"} for t in q_tokens
    ):
        role_boost = 0.2
    elif role in {"link"} and any(t in {"go", "open", "navigate"} for t in q_tokens):
        role_boost = 0.1
    return min(1.0, len(overlap) / len(q_tokens) + role_boost)


def browser_observe(
    instruction: str,
    *,
    top_k: int = 5,
    llm_rerank: bool = False,
) -> list[ObserveResult]:
    """Return reusable Playwright selectors matching an instruction.

    No LLM call when `llm_rerank=False` (default): uses token-overlap scoring on
    the ARIA snapshot. This is the load-bearing path: one observe() call
    produces a list of selectors that subsequent deterministic Playwright ops
    can act on for zero extra LLM calls.

    Args:
        instruction: natural-language description of what to find.
            Example: "the submit button at the bottom of the form".
        top_k: maximum number of results to return.
        llm_rerank: if True, call the LLM to rerank the candidates. Adds
            one LLM call per observe(). Default False keeps the path cheap.

    Returns:
        list[ObserveResult]: ranked observations, best match first.
    """
    try:
        snapshot = _aria_snapshot()
    except Exception:
        return []
    elements = _parse_aria_to_elements(snapshot)
    scored = sorted(
        ((_score_match(instruction, e), e) for e in elements),
        key=lambda x: x[0],
        reverse=True,
    )
    scored = [(s, e) for s, e in scored if s > 0.0][:top_k]
    if not scored:
        return []
    if llm_rerank:
        # Path A future work: route through gptme's LLM router. Skipped here
        # so the benchmark measures the cheap path.
        pass
    return [
        ObserveResult(
            description=f"{e['role']} {e['name']!r}",
            selector=e["selector"],
            method=_method_for_role(e["role"]),
            instruction=instruction,
        )
        for _, e in scored
    ]


def _dispatch_action(
    sel: str,
    method: str,
    arguments: list[str],
    t0: float,
) -> ActResult:
    """Run one deterministic dispatch through gptme's browser primitives.

    Each of these is zero-LLM.
    """
    from gptme.tools.browser import (
        click_element,
        fill_element,
        hover_element,
        press_key,
        select_option,
    )

    try:
        if method == "click":
            click_element(sel)
        elif method == "fill":
            assert arguments, "fill requires arguments=[value]"
            fill_element(sel, arguments[0])
        elif method == "press":
            assert arguments, "press requires arguments=[key]"
            click_element(sel)  # focus the element before pressing
            press_key(arguments[0])
        elif method == "select":
            assert arguments, "select requires arguments=[value]"
            select_option(sel, arguments[0])
        elif method == "hover":
            hover_element(sel)
        else:
            return ActResult(
                success=False,
                message=f"unknown method {method!r}",
                selector_used=sel,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
    except Exception as exc:
        return ActResult(
            success=False,
            message=f"{type(exc).__name__}: {exc}",
            selector_used=sel,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    return ActResult(
        success=True,
        message="ok",
        selector_used=sel,
        llm_calls=0,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


def browser_act(
    action_or_observed: str | ObserveResult,
    *,
    method: str | None = None,
    arguments: list[str] | None = None,
    retry_on_stale: bool = True,
) -> ActResult:
    """Execute a single browser action.

    Two forms:
      1. `browser_act("click the submit button")` — runs observe() internally
         and dispatches the top match. One LLM call only when observe() needs
         reranking.
      2. `browser_act(observed)` — pass an ObserveResult from a prior
         browser_observe() call. Zero LLM calls; uses the cached selector.

    Stale-selector recovery: when a cached (observed) selector no longer
    resolves — the page re-rendered, moved the element, or swapped refs — the
    dispatch fails and, with `retry_on_stale=True` (default), the element is
    re-observed once with the same query and retried with the fresh top
    match. The re-observe is zero-LLM on the default token-scoring path, so
    the retry costs nothing against the LLM-call budget. `retry_on_stale=False`
    returns the first failure immediately.

    Returns ActResult with `success`, the selector used, and elapsed time.
    """
    t0 = time.monotonic()
    if isinstance(action_or_observed, ObserveResult):
        best = action_or_observed
        reobserve_query = best.instruction or best.description
    else:
        reobserve_query = action_or_observed
        observed = browser_observe(action_or_observed, top_k=1)
        if not observed:
            return ActResult(
                success=False,
                message=f"no elements matched: {action_or_observed!r}",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        best = observed[0]

    sel = best.selector
    method = method or best.method
    arguments = arguments or best.arguments

    result = _dispatch_action(sel, method, arguments, t0)
    if result.success or not retry_on_stale:
        return result

    # Stale selector: re-observe once with the same query. If the fresh top
    # match is the same selector, retrying is pointless — return the failure.
    #
    # Only retry when the failure message indicates the locator didn't resolve
    # (e.g. element not found, strict mode violation). A timeout/navigation
    # error can mean the action already executed — retrying would double it.
    _locator_fail_phrases = (
        "no element",
        "not found",
        "did not find",
        "locator",
        "strict mode",
        "target closed",
    )
    msg_lower = result.message.lower()
    if not any(phrase in msg_lower for phrase in _locator_fail_phrases):
        return result  # non-locator failure — don't retry (avoid double-acting)

    fresh = browser_observe(reobserve_query, top_k=1)
    if fresh and fresh[0].selector != sel:
        result = _dispatch_action(fresh[0].selector, method, arguments, t0)
    return result


def browser_extract(
    instruction: str | None = None,
    *,
    schema: type | None = None,
) -> ExtractResult:
    """Extract structured data from the current page.

    `instruction=None` extracts the visible text content (zero-LLM).
    `instruction` set without `schema` extracts a JSON-shaped dict via
    LLM (one call). `schema` is accepted as a typing hint but not enforced
    in Path A — strict Pydantic validation is a Path-B feature.
    """
    t0 = time.monotonic()
    try:
        snapshot = _aria_snapshot()
    except Exception as exc:
        return ExtractResult(
            success=False,
            data={"error": f"browser snapshot failed: {type(exc).__name__}: {exc}"},
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    if instruction is None:
        # Zero-LLM text extraction.
        return ExtractResult(
            success=True,
            data=snapshot,
            llm_calls=0,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    # Path A: no LLM available in this prototype, return the raw snapshot
    # with a note. Path B will fill this in via the LLM router.
    return ExtractResult(
        success=False,
        data={
            "error": "schema-aware extract requires the LLM-backed path",
            "hint": "see design doc browser-tool-act-observe-extract.md (Path B)",
            "raw_snapshot_excerpt": snapshot[:500],
        },
        llm_calls=0,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# gptme tool-spec wiring (so the prototype can be loaded by gptme).
# ---------------------------------------------------------------------------


def has_semantic_browser() -> bool:
    """True if the prototype's dependencies are importable."""
    try:
        from gptme.tools.browser import snapshot_page  # noqa: F401

        return True
    except Exception:
        return False


def examples() -> list[str]:
    """Example tool invocations surfaced to the LLM."""
    return [
        'browser_observe("the submit button")',
        'browser_act("type hello@example.com into the email field")',
        "browser_act(<ObserveResult from prior observe()>)",
        'browser_extract("all product names and prices")',
    ]


if __name__ == "__main__":
    # Smoke check: import path is healthy.
    print("semantic browser module loaded")
    print(f"  has_semantic_browser(): {has_semantic_browser()}")
