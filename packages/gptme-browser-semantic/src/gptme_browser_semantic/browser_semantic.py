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
        r'^[\s\-]*(?P<role>[\w-]+)\s+"(?P<name>(?:[^"\\]|\\.)*)"(?:\s+\[(?P<ref>[^\]]+)\])?'
    )
    for line in snapshot.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        role = m.group("role").strip()
        # Unescape only the two escape sequences the ARIA snapshot uses: \" and \\.
        # A broad r"\\(.)" would corrupt names with literal \n, Windows paths, etc.
        name = re.sub(r'\\("|\\)', r"\1", m.group("name")).strip()
        ref = m.group("ref") or ""
        if not name:
            continue
        # Only treat bracket content as a real ref if it is a `ref=...`
        # attribute (e.g. "ref=e1").  Other bracket attributes like
        # "[level=2]" must be ignored per the README spec.
        # Also handle multi-attribute brackets like "[ref=e5, level=1]" — extract
        # only the "ref=VALUE" token and discard the rest.
        actual_ref = next(
            (tok.strip() for tok in ref.split(",") if tok.strip().startswith("ref=")),
            "",
        )
        # Escape single and double quotes so the role-name selector stays valid
        # for names like "John's" or 'say "hello"'.
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        selector = (
            f"[{actual_ref}]" if actual_ref else f"role={role}[name='{escaped_name}']"
        )
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
    # Include the element's ARIA role so a query like "button" or "link" matches
    # elements by role, not only by name token overlap.
    e_tokens = {t.lower() for t in re.findall(r"\w+", element["name"])} | {
        element["role"].lower()
    }
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
        raise NotImplementedError(
            "llm_rerank is a Path-B feature; not available in Path A"
        )
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
    element_method: str = "click",
) -> ActResult:
    """Run one deterministic dispatch through gptme's browser primitives.

    Each of these is zero-LLM.
    """
    try:
        from gptme.tools.browser import (
            click_element,
            fill_element,
            hover_element,
            press_key,
            select_option,
        )
    except ImportError as exc:
        return ActResult(
            success=False,
            message=f"gptme browser backend not available: {exc}",
            selector_used=sel,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    try:
        if method == "click":
            click_element(sel)
        elif method == "fill":
            assert arguments, "fill requires arguments=[value]"
            fill_element(sel, arguments[0])
        elif method == "press":
            assert arguments, "press requires arguments=[key]"
            # Focus the element before pressing — only valid for input-type elements
            # (textbox, searchbox, combobox) where a click merely moves cursor focus.
            # Button/link elements (element_method=="click") cannot be focused without
            # activating them: gptme's browser tool has no focus-only primitive, and
            # calling click_element would double-fire the activation. Use method='click'
            # to activate those elements instead.
            if element_method == "click":
                return ActResult(
                    success=False,
                    message=(
                        f"press is not supported for {element_method!r}-type elements "
                        "(no focus-only primitive in gptme browser tool); "
                        "use method='click' to activate buttons/links"
                    ),
                    selector_used=sel,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
            click_element(sel)
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

    # Detect the common mistake: a fill element was selected (string-form or
    # ObserveResult) but the caller provided no value. Surface a clear error
    # instead of the opaque AssertionError from `_dispatch_action`.
    if method == "fill" and not arguments:
        return ActResult(
            success=False,
            message=(
                f"element '{best.description}' needs a fill value; "
                "use browser_act(observed, arguments=['value']) or pass arguments=['value']"
            ),
            selector_used=sel,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    element_method = best.method
    result = _dispatch_action(sel, method, arguments, t0, element_method=element_method)
    if result.success or not retry_on_stale:
        return result

    # Stale selector: re-observe once with the same query. If the fresh top
    # match is the same selector, retrying is pointless — return the failure.
    #
    # Block retry only when the error signals that the action already executed
    # (navigation or detach after click). Any other failure — including a plain
    # "Timeout Xms exceeded." where Playwright omits the call log — means the
    # locator never resolved, so retrying is safe.
    #
    # Inverted guard: exclude known post-action phrases rather than requiring
    # locator-specific phrases. gptme's browser backend may surface only the first
    # line of Playwright's TimeoutError, dropping "waiting for locator(...)" from
    # the message, so whitelist-based phrase matching misses those cases.
    _no_retry_phrases = (
        "not attached",
        "detached",
        "target closed",
        "navigation",
    )
    msg_lower = result.message.lower()
    if any(phrase in msg_lower for phrase in _no_retry_phrases):
        return result  # post-action failure — don't retry (avoid double-acting)

    fresh = browser_observe(reobserve_query, top_k=1)
    if fresh:
        # Retry with the fresh selector. When the selector is unchanged the element
        # was temporarily absent (transient DOM change) and is visible again per the
        # snapshot — retrying is correct. When fresh is empty, the element is truly
        # absent and there is nothing to retry against.
        result = _dispatch_action(
            fresh[0].selector, method, arguments, t0, element_method=fresh[0].method
        )
    return result


def browser_extract(
    instruction: str | None = None,
    *,
    schema: type | None = None,
) -> ExtractResult:
    """Extract structured data from the current page.

    In Path A, `instruction` is always ignored and the raw ARIA snapshot is
    returned regardless. LLM-backed instruction-based extraction is a Path-B
    feature only. `schema` is rejected in Path A with a clear error.
    """
    t0 = time.monotonic()
    if schema is not None:
        return ExtractResult(
            success=False,
            data={
                "error": "schema-aware extraction is a Path-B feature and not available in Path A",
                "hint": "remove schema= and parse the raw snapshot yourself, or wait for Path B",
            },
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    try:
        snapshot = _aria_snapshot()
    except Exception as exc:
        return ExtractResult(
            success=False,
            data={"error": f"browser snapshot failed: {type(exc).__name__}: {exc}"},
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    # Path A always returns the raw ARIA snapshot regardless of instruction.
    # Instruction-based LLM extraction is a Path-B feature; Path A ignores the
    # instruction and returns the full snapshot so callers can filter it themselves.
    return ExtractResult(
        success=True,
        data=snapshot,
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
