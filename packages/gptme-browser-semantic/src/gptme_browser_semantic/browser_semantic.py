"""
Semantic browser primitives for gptme computer-use.

Implements the stagehand act/observe/extract pattern as three library functions:
  - browser_act: execute a deterministic action on the current page
  - browser_observe: return Playwright selectors matching an instruction (reusable)
  - browser_extract: return the current raw ARIA snapshot

Design intent (full justification in the "browser tool act/observe/extract"
design doc, knowledge/technical-designs/browser-tool-act-observe-extract.md
in the workspace that prototyped this):

- observe() is the load-bearing primitive: it returns a list of ObserveResult
  objects, each with a Playwright selector and a description. Once you have the
  selector, deterministic Playwright ops (`page.click`, `page.fill`) are zero-LLM
  calls. This is the headline gain over the existing `browser.snapshot` →
  `click_element(selector)` pattern, which already does this for ONE action at
  a time; observe() enables chained deterministic actions from one snapshot.
- act() with a pre-observed ObserveResult also goes deterministic (no LLM call).
  Reusing observed selectors across actions is the multi-step efficiency win.
- extract() returns raw ARIA. Schema-aware extraction is deliberately rejected
  in Path A rather than silently pretending to validate structured output.

This module is the prototype. There are two implementation paths:

  Path A (this file): the *interface*. gptme tools are pure-Python functions
    that take the existing playwright page from `browser.py`. The semantic
    primitives are implemented over Playwright directly: observe() walks the
    ARIA tree and uses deterministic token-overlap ranking; act() runs the
    selector; extract() returns the snapshot.
    Tradeoff: no stagehand dependency, but no LLM reranking or typed extraction.

  Stagehand evaluation path (in the design doc): stagehand==3.23.0 now has a
    local server and local browser, but it requires a supported CDP/session
    handoff to share gptme's current page. It is not a mechanical backend swap.

This module implements Path A. The bundled benchmark is static scenario
accounting, not an executed performance benchmark.

Install notes:
- Requires `pip install playwright` (already part of `gptme[browser]` extras).
- `playwright install chromium` must have been run.
- LLM-backed reranking and schema extraction are not implemented in Path A.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# Reuse gptme's existing playwright page. The browser tool exposes
# `snapshot_page()` which returns an ARIA accessibility snapshot (string).
# We don't take a hard import on browser.py here so this module can be
# loaded standalone for the prototype harness.


# Playwright's positional ordinal, anchored at the end of the selector. A
# page-controlled accessible name may contain the literal text " >> nth=", so a
# substring test would misclassify a unique element as a duplicate.
_POSITIONAL_SELECTOR_RE = re.compile(r" >> nth=\d+$")


def _is_positional(selector: str) -> bool:
    """True when the selector ends in Playwright's ``>> nth=N`` ordinal."""
    return _POSITIONAL_SELECTOR_RE.search(selector) is not None


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
    # True when the snapshot held more than one element with this role+name, so
    # the selector (an ordinal, or a ref among twins) is not a unique identity.
    ambiguous: bool = False


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


def _parse_aria_to_elements(snapshot: str) -> list[dict[str, Any]]:
    """Parse the gptme ARIA snapshot format into a flat element list.

    gptme's snapshot_page returns a human-readable tree with roles + names +
    optional selectors like `[ref=e5]`. We extract every (role, name, ref)
    triple into a flat list so observe() can rank them.
    """
    elements: list[dict[str, Any]] = []
    # Lines look like:
    #   - link "Home" [ref=e1]
    #   - button "Submit" [ref=e3]
    #   - textbox "Email" [ref=e7]
    #   - link /r/rust/:               (names delimited by "/" are unquoted)
    #   - 'link "Show HN: My project"':  (YAML-quoted when the name has ": ", " #", ...)
    item_re = re.compile(r"^(?P<indent>\s*)-\s+(?P<rest>.*)$")
    line_re = re.compile(
        r'^(?P<role>[\w-]+)\s+(?:"(?P<name>(?:[^"\\]|\\.)*)"|(?P<slash>/\S*/)(?=:|\s|$))'
        r"(?:\s+\[(?P<ref>[^\]]+)\])?"
    )
    # Split on "\n" only: str.splitlines() also splits on \x0b etc., which a
    # page title can contain, letting the page fabricate element lines.
    #
    # A literal "\n" in a title cannot do that: the document.title getter
    # collapses ASCII whitespace, so page.title() returns "x - button ..." for
    # document.title = 'x\n- button ...'. Only \x0b survives the getter, and it
    # no longer starts a line here. Measured in Chromium 1255.
    lines = snapshot.split("\n")
    for line in lines:
        item = item_re.match(line)
        if not item:
            continue
        rest = item.group("rest")
        if rest.startswith("'"):
            # YAML single-quoted scalar: '' is an escaped quote.
            end = re.match(r"'((?:[^']|'')*)'", rest)
            if not end:
                continue
            rest = end.group(1).replace("''", "'")
        m = line_re.match(rest)
        if not m:
            continue
        role = m.group("role").strip()
        if m.group("slash") is not None:
            name = m.group("slash")
        else:
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
        # Option descendants do not identify a native <select>: editable ARIA
        # comboboxes can have them too. Keep fill as the default; callers with
        # a native select must pass method="select" to browser_act.
        elements.append({"role": role, "name": name, "selector": selector})

    # Several elements with the same role+name share one role selector, which
    # resolves to multiple elements (a strict-mode violation on click). Make
    # each one target its own match.
    counts: dict[str, int] = {}
    for e in elements:
        counts[e["selector"]] = counts.get(e["selector"], 0) + 1
    # role+name twins are indistinguishable even when refs give them distinct
    # selectors, so ambiguity is keyed on role+name, not on the selector.
    twins: dict[tuple[str, str], int] = {}
    for e in elements:
        key = (e["role"], e["name"])
        twins[key] = twins.get(key, 0) + 1
    seen: dict[str, int] = {}
    for e in elements:
        sel = e["selector"]
        if counts[sel] > 1 and sel.startswith("role="):
            e["selector"] = f"{sel} >> nth={seen.get(sel, 0)}"
            seen[sel] = seen.get(sel, 0) + 1
        e["ambiguous"] = twins[(e["role"], e["name"])] > 1
    return elements


def _method_for_role(role: str) -> str:
    """Return the default interaction method for an ARIA role."""
    r = role.lower()
    if r in {"textbox", "searchbox", "combobox", "spinbutton"}:
        return "fill"
    if r in {"select", "listbox"}:
        return "select"
    return "click"


def _score_match(query: str, element: dict[str, Any]) -> float:
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
        llm_rerank: reserved for a future native reranker. Path A raises
            ``NotImplementedError`` when this is True.

    Returns:
        list[ObserveResult]: ranked observations, best match first.
    """
    if llm_rerank:
        raise NotImplementedError(
            "llm_rerank is a Path-B feature; not available in Path A"
        )
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
    return [
        ObserveResult(
            description=f"{e['role']} {e['name']!r}",
            selector=e["selector"],
            method=_method_for_role(e["role"]),
            instruction=instruction,
            ambiguous=bool(e["ambiguous"]),
        )
        for _, e in scored
    ]


def _find_same_element(
    chosen: ObserveResult, fresh: list[ObserveResult]
) -> ObserveResult | None:
    """Find the element the caller chose among fresh observations.

    Returns None when it cannot be identified, so a retry never acts on a
    lookalike or on whichever element merely ranks first.
    """
    # A duplicate is not an identity. Twins share role and accessible name, so
    # neither a positional ``>> nth=i`` ordinal (DOM order can change) nor a
    # ``[ref=eN]`` (refs are reassigned on re-render, and a vanished twin's ref
    # may be reused by a lookalike) says which one the caller chose. Never retry
    # a duplicate; return the first failure instead.
    if chosen.ambiguous or _is_positional(chosen.selector):
        return None
    same = [r for r in fresh if r.description == chosen.description]
    for result in same:
        if result.selector == chosen.selector:
            return result
    # A changed selector is only unambiguous for a single match.
    if len(same) == 1:
        return same[0]
    return None


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
      1. `browser_act("click the submit button")` — runs the deterministic
         observe() scorer internally and dispatches the top match.
      2. `browser_act(observed)` — pass an ObserveResult from a prior
         browser_observe() call. Zero LLM calls; uses the cached selector.

    Stale-selector recovery: when a cached (observed) selector no longer
    resolves — the page re-rendered, moved the element, or swapped refs — the
    dispatch fails and, with `retry_on_stale=True` (default), the page is
    re-observed once with the same query and the same element is retried: same
    role and name, and either the same selector or the only such match. If it
    cannot be identified, the first failure is returned. A duplicate role/name
    pair is never retried: twins are indistinguishable, so neither a positional
    ``>> nth=i`` ordinal nor a ``[ref=eN]`` identifies which one the caller
    chose after a re-render, and the retry could act on a lookalike. The
    re-observe makes no model call on the token-scoring path.
    `retry_on_stale=False` returns the first failure immediately.

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

    # Stale selector: re-observe once with the same query and look for the
    # element the caller chose.
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
        # gptme's click runs the action, then snapshots; a failed post-action
        # snapshot means the click already happened.
        "could not get accessibility snapshot",
    )
    msg_lower = result.message.lower()
    # A refusal (e.g. the user denied a sensitive fill) must not re-prompt.
    # _dispatch_action formats the message as f"{type(exc).__name__}: {exc}", so
    # anchor on the full "PermissionError:" prefix: a bare startswith() would
    # also suppress an unrelated exception whose name merely begins that way.
    if result.message.startswith("PermissionError:"):
        return result
    if any(phrase in msg_lower for phrase in _no_retry_phrases):
        return result  # post-action failure — don't retry (avoid double-acting)
    # A duplicate is never retried, so skip the re-observe entirely rather than
    # paying a browser round-trip whose result _find_same_element would discard.
    if best.ambiguous or _is_positional(best.selector):
        return result

    target = _find_same_element(
        best, browser_observe(reobserve_query, top_k=sys.maxsize)
    )
    if target:
        # When the selector is unchanged the element was temporarily absent
        # (transient DOM change) and is visible again per the snapshot, so
        # retrying is correct.
        result = _dispatch_action(
            target.selector, method, arguments, t0, element_method=target.method
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
        'browser_act(browser_observe("the email field")[0], arguments=["hello@example.com"])',
        "browser_act(<ObserveResult from prior observe()>)",
        'browser_extract("all product names and prices")',
    ]


if __name__ == "__main__":
    # Smoke check: import path is healthy.
    print("semantic browser module loaded")
    print(f"  has_semantic_browser(): {has_semantic_browser()}")
