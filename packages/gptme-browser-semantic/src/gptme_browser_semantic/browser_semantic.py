"""
Semantic browser primitives for gptme computer-use.

Wraps the stagehand act/observe/extract pattern as three functions:
  - browser_observe: return reusable Playwright selectors matching an instruction
  - browser_act: execute an action, reusing a prior observation when given
  - browser_extract: raw ARIA text (Path A) or a Path-B typed-extract stub

Path A (this module) is a pure-Python layer over gptme's existing
``browser.snapshot_page()`` ARIA output and Playwright dispatch helpers.
No stagehand dependency, no new browser-launching code.

Path B (blocked): wrap ``stagehand.local_browser.launch()`` once the Python
SDK exposes a usable local-only mode. The ``ObserveResult`` shape is
deliberately stagehand-compatible so that swap is mechanical.

Observe is the load-bearing primitive: one call produces ranked selectors
that subsequent deterministic Playwright ops act on for zero extra
interpretation cost. The default scorer is token-overlap + role boost
(no LLM). ``llm_rerank=True`` is reserved for the residual ambiguous
case and is not wired in Path A.

Install notes:
- Requires ``playwright`` (already part of ``gptme[browser]`` extras).
- ``playwright install chromium`` must have been run.
- For a future LLM-backed observe/extract path, gptme's existing model
  client is the intended router. Path A does not call an LLM.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Reuse gptme's existing playwright page via lazy import so this module can
# be loaded standalone (tests stub ``gptme.tools.browser`` in sys.modules).


_LINE_RE = re.compile(r'^[\s\-]*(?P<role>\w+)\s+"(?P<name>[^"]*)"')
_REF_RE = re.compile(r"\[ref=([^\]]+)\]")

_FILL_ROLES = {"textbox", "searchbox", "spinbutton"}
_SELECT_ROLES = {"combobox", "listbox"}


@dataclass
class ObserveResult:
    """A single Playwright-anchored observation returned by browser_observe.

    Mirrors stagehand's ObserveResult shape so a future Path-B swap is mechanical.
    """

    description: str
    selector: str
    method: str = "click"  # click | fill | hover | select | press
    arguments: list[str] = field(default_factory=list)


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
    """Fetch the current page's ARIA snapshot via gptme's browser tool."""
    from gptme.tools.browser import snapshot_page

    snapshot: str = snapshot_page()
    return snapshot


def _default_method(role: str) -> str:
    role_l = role.lower()
    if role_l in _FILL_ROLES:
        return "fill"
    if role_l in _SELECT_ROLES:
        return "select"
    return "click"


def _selector_for(role: str, name: str, ref: str) -> str:
    """Build a Playwright selector gptme's click_element/fill_element accept.

    gptme's live ``snapshot_page()`` uses Playwright's default aria snapshot,
    which does **not** currently emit ``[ref=eN]``. When a ref *is* present
    (tests, or a future gptme snapshot with ``ref=True``), keep it so
    stale-ref recovery can be exercised. Otherwise emit a role+name locator,
    which is what gptme documents (``role=button[name='Submit']``).
    """
    if ref:
        return f"[ref={ref}]"
    if "'" not in name:
        return f"role={role}[name='{name}']"
    return f"text={name}"


def _parse_aria_to_elements(snapshot: str) -> list[dict[str, str]]:
    """Parse the gptme ARIA snapshot format into a flat element list.

    Lines look like::

        - link "Home" [ref=e1]
        - button "Submit" [level=1]
        - textbox "Email"

    Only ``[ref=...]`` is treated as a selector hint; other bracket
    attributes (``[level=1]``, ``[disabled]``) are ignored so they cannot
    become a non-unique CSS selector.
    """
    elements: list[dict[str, str]] = []
    for line in snapshot.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        role = m.group("role").strip()
        name = m.group("name").strip()
        if not name:
            continue
        ref_m = _REF_RE.search(line)
        ref = ref_m.group(1) if ref_m else ""
        elements.append(
            {
                "role": role,
                "name": name,
                "selector": _selector_for(role, name, ref),
            }
        )
    return elements


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
    elif role in _FILL_ROLES and any(
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

    No LLM call when ``llm_rerank=False`` (default): uses token-overlap scoring
    on the ARIA snapshot. This is the load-bearing path: one observe() call
    produces a list of selectors that subsequent deterministic Playwright ops
    can act on for zero extra LLM calls.

    Args:
        instruction: natural-language description of what to find.
            Example: "the submit button at the bottom of the form".
        top_k: maximum number of results to return.
        llm_rerank: if True, call the LLM to rerank the candidates. Adds
            one LLM call per observe(). Default False keeps the path cheap.
            Path A leaves this as a no-op hook.

    Returns:
        list[ObserveResult]: ranked observations, best match first. Ties
        keep document order (stable sort).
    """
    snapshot = _aria_snapshot()
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
        # so the cheap path stays measurable without a provider.
        pass
    return [
        ObserveResult(
            description=f"{e['role']} {e['name']!r}",
            selector=e["selector"],
            method=_default_method(e["role"]),
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
            if not arguments:
                return ActResult(
                    success=False,
                    message="fill requires arguments=[value]",
                    selector_used=sel,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
            fill_element(sel, arguments[0])
        elif method == "press":
            if not arguments:
                return ActResult(
                    success=False,
                    message="press requires arguments=[key]",
                    selector_used=sel,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
            press_key(arguments[0])
        elif method == "select":
            if not arguments:
                return ActResult(
                    success=False,
                    message="select requires arguments=[value]",
                    selector_used=sel,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
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
      1. ``browser_act("click the submit button")`` — runs observe() internally
         and dispatches the top match. One LLM call only when observe() needs
         reranking.
      2. ``browser_act(observed)`` — pass an ObserveResult from a prior
         browser_observe() call. Zero LLM calls; uses the cached selector.

    Stale-selector recovery: when a cached (observed) selector no longer
    resolves — the page re-rendered, moved the element, or swapped refs — the
    dispatch fails and, with ``retry_on_stale=True`` (default), the element is
    re-observed once with the same query and retried with the fresh top
    match. The re-observe is zero-LLM on the default token-scoring path, so
    the retry costs nothing against the LLM-call budget. ``retry_on_stale=False``
    returns the first failure immediately.

    Returns ActResult with ``success``, the selector used, and elapsed time.
    """
    t0 = time.monotonic()
    if isinstance(action_or_observed, ObserveResult):
        best = action_or_observed
        reobserve_query = best.description
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
    fresh = browser_observe(reobserve_query, top_k=1)
    if fresh and fresh[0].selector != sel:
        result = _dispatch_action(fresh[0].selector, method, arguments, t0)
    return result


def browser_extract(
    instruction: str | None = None,
    *,
    schema: type | None = None,
) -> ExtractResult:
    """Extract data from the current page.

    Path A returns the raw ARIA snapshot (zero-LLM) whether or not
    ``instruction`` is set. The instruction is recorded for the caller; it
    is not interpreted. Schema-aware typed extract is Path B and is
    rejected rather than silently fabricating structured data.
    """
    t0 = time.monotonic()
    snapshot = _aria_snapshot()
    if schema is not None:
        return ExtractResult(
            success=False,
            data={
                "error": "schema-aware extract requires the LLM-backed path",
                "hint": "see design doc browser-tool-act-observe-extract.md (Path B)",
                "instruction": instruction,
                "raw_snapshot_excerpt": snapshot[:500],
            },
            llm_calls=0,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    # Zero-LLM text extraction. Instruction is advisory; Path A does not
    # filter the snapshot.
    return ExtractResult(
        success=True,
        data=snapshot,
        llm_calls=0,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )


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
    print("semantic browser module loaded")
    print(f"  has_semantic_browser(): {has_semantic_browser()}")
