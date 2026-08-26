"""Tests for the Path A semantic browser primitives.

No live browser: ``gptme.tools.browser`` is stubbed in ``sys.modules`` so
observe/act/extract run against a recorded ARIA snapshot and a recording
dispatch layer. The suite is therefore deterministic and dependency-free.
"""

from __future__ import annotations

import sys
import types

import pytest
from gptme_browser_semantic import (
    ObserveResult,
    browser_act,
    browser_extract,
    browser_observe,
)

SNAPSHOT_V1 = """\
- link "Home" [ref=e1]
- textbox "Search the news" [ref=e2]
- button "Search" [ref=e3]
- link "First story" [ref=e4]
- button "Submit" [ref=e5]
- button "Submit again" [ref=e6]
- textbox "Email" [ref=e7]
"""

# Re-render: the "Submit" button got a new ref, everything else stable.
SNAPSHOT_SUBMIT_MOVED = SNAPSHOT_V1.replace("[ref=e5]", "[ref=e9]")
# Re-render: the search box moved.
SNAPSHOT_SEARCH_MOVED = SNAPSHOT_V1.replace(
    '- textbox "Search the news" [ref=e2]',
    '- textbox "Search the news" [ref=e8]',
)


class BrowserStub:
    """Recording stand-in for ``gptme.tools.browser``.

    ``snapshots`` is a queue: every ``snapshot_page()`` call pops the next
    snapshot, and the last one sticks once the queue is exhausted (models a
    stable page).

    ``custom_errors`` maps a selector to a specific exception to raise instead
    of the default ``TimeoutError("locator '...' not found")``.  Use this to
    test failure types that should NOT trigger the stale-selector retry.
    """

    def __init__(
        self, snapshots: list[str], *, raise_on_snapshot: bool = False
    ) -> None:
        self.snapshots = snapshots
        self.snapshot_calls = 0
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.fail_selectors: set[str] = set()
        self.custom_errors: dict[str, Exception] = {}
        self.raise_on_snapshot = raise_on_snapshot

    def snapshot_page(self) -> str:
        if self.raise_on_snapshot:
            raise RuntimeError("browser page closed")
        self.snapshot_calls += 1
        idx = min(self.snapshot_calls - 1, len(self.snapshots) - 1)
        return self.snapshots[idx]

    def click_element(self, selector: str) -> None:
        # Record the attempt *before* the failure check: tests assert on the
        # dispatch sequence, including the stale attempt that raised.
        self.clicks.append(selector)
        if selector in self.custom_errors:
            raise self.custom_errors[selector]
        if selector in self.fail_selectors:
            raise TimeoutError(f"locator '{selector}' not found")

    def fill_element(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))
        if selector in self.fail_selectors:
            raise TimeoutError(f"locator '{selector}' not found")

    def press_key(self, key: str) -> None:
        pass

    def select_option(self, selector: str, value: str) -> None:  # pragma: no cover
        pass

    def hover_element(self, selector: str) -> None:  # pragma: no cover - passthrough
        pass


def _wire_stub(stub: BrowserStub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire a BrowserStub into sys.modules so the semantic module uses it."""
    browser_mod = types.ModuleType("gptme.tools.browser")
    setattr(browser_mod, "snapshot_page", stub.snapshot_page)
    setattr(browser_mod, "click_element", stub.click_element)
    setattr(browser_mod, "fill_element", stub.fill_element)
    setattr(browser_mod, "press_key", stub.press_key)
    setattr(browser_mod, "select_option", stub.select_option)
    setattr(browser_mod, "hover_element", stub.hover_element)
    tools_mod = types.ModuleType("gptme.tools")
    setattr(tools_mod, "browser", browser_mod)
    gptme_mod = types.ModuleType("gptme")
    setattr(gptme_mod, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "gptme", gptme_mod)
    monkeypatch.setitem(sys.modules, "gptme.tools", tools_mod)
    monkeypatch.setitem(sys.modules, "gptme.tools.browser", browser_mod)


@pytest.fixture
def browser_stub(monkeypatch: pytest.MonkeyPatch) -> BrowserStub:
    stub = BrowserStub([SNAPSHOT_V1])
    _wire_stub(stub, monkeypatch)
    return stub


@pytest.fixture
def failing_browser_stub(monkeypatch: pytest.MonkeyPatch) -> BrowserStub:
    """A stub whose snapshot_page raises RuntimeError (browser closed)."""
    stub = BrowserStub([], raise_on_snapshot=True)
    _wire_stub(stub, monkeypatch)
    return stub


# ---------------------------------------------------------------------------
# browser_observe — ranking
# ---------------------------------------------------------------------------


def test_observe_ranks_best_match_first(browser_stub: BrowserStub) -> None:
    results = browser_observe("type into the search box")
    assert results, "expected at least one match"
    assert results[0].selector == "[ref=e2]"


def test_observe_top_k_limits_results(browser_stub: BrowserStub) -> None:
    results = browser_observe("type into the search box", top_k=1)
    assert [r.selector for r in results] == ["[ref=e2]"]


def test_observe_no_match_returns_empty(browser_stub: BrowserStub) -> None:
    # Zero token overlap with every element name, and no role keyword.
    assert browser_observe("purple elephant") == []


def test_observe_results_are_reusable_observations(browser_stub: BrowserStub) -> None:
    results = browser_observe("the first story link", top_k=1)
    result = results[0]
    assert isinstance(result, ObserveResult)
    assert result.selector == "[ref=e4]"
    assert result.method == "click"


# ---------------------------------------------------------------------------
# browser_observe — ambiguous labels
# ---------------------------------------------------------------------------


def test_observe_surfaces_all_ambiguous_matches_deterministically(
    browser_stub: BrowserStub,
) -> None:
    first = browser_observe("submit button")
    second = browser_observe("submit button")
    # Same snapshot in => identical ranking out (no hidden nondeterminism).
    selectors = [r.selector for r in first]
    assert selectors == [r.selector for r in second]
    # The two "Submit"-labelled buttons are the top two, tied, and ties
    # resolve in document order (stable sort).
    assert selectors[:2] == ["[ref=e5]", "[ref=e6]"]
    # The "Search" button also surfaces: the role boost fires on the
    # "submit" keyword even without name overlap. Pin that behavior.
    assert set(selectors) == {"[ref=e5]", "[ref=e6]", "[ref=e3]"}


def test_act_string_form_dispatches_first_ambiguous_match(
    browser_stub: BrowserStub,
) -> None:
    result = browser_act("click submit")
    assert result.success
    assert result.selector_used == "[ref=e5]"
    assert browser_stub.clicks == ["[ref=e5]"]


# ---------------------------------------------------------------------------
# browser_act — string form basics
# ---------------------------------------------------------------------------


def test_act_string_form_observes_then_dispatches(browser_stub: BrowserStub) -> None:
    result = browser_act("open the first story")
    assert result.success
    assert result.selector_used == "[ref=e4]"
    assert browser_stub.clicks == ["[ref=e4]"]


def test_act_no_match_fails_without_dispatch(browser_stub: BrowserStub) -> None:
    # Zero token overlap with every element name, and no role keyword.
    result = browser_act("purple elephant")
    assert not result.success
    assert "no elements matched" in result.message
    assert browser_stub.clicks == []
    assert browser_stub.fills == []


def test_act_unknown_method_fails(browser_stub: BrowserStub) -> None:
    result = browser_act("open the first story", method="dance")
    assert not result.success
    assert "unknown method" in result.message
    assert browser_stub.clicks == []


# ---------------------------------------------------------------------------
# browser_act — stale selectors + re-observe-on-failure
# ---------------------------------------------------------------------------


def _observe_submit(browser_stub: BrowserStub) -> ObserveResult:
    observed = browser_observe("submit button", top_k=1)
    assert observed
    return observed[0]


def test_stale_selector_reobserves_and_retries(browser_stub: BrowserStub) -> None:
    observed = _observe_submit(browser_stub)
    assert observed.selector == "[ref=e5]"
    # Page re-renders: the observed ref no longer resolves; the button now
    # lives at [ref=e9].
    browser_stub.snapshots = [SNAPSHOT_SUBMIT_MOVED]
    browser_stub.fail_selectors.add("[ref=e5]")

    result = browser_act(observed)

    assert result.success
    assert result.selector_used == "[ref=e9]"
    assert browser_stub.clicks == ["[ref=e5]", "[ref=e9]"]


def test_stale_selector_retry_disabled_returns_first_failure(
    browser_stub: BrowserStub,
) -> None:
    observed = _observe_submit(browser_stub)
    browser_stub.snapshots = [SNAPSHOT_SUBMIT_MOVED]
    browser_stub.fail_selectors.add("[ref=e5]")

    result = browser_act(observed, retry_on_stale=False)

    assert not result.success
    assert result.selector_used == "[ref=e5]"
    # Exactly one dispatch — no retry happened.
    assert browser_stub.clicks == ["[ref=e5]"]


def test_stale_selector_retry_is_skipped_when_match_unchanged(
    browser_stub: BrowserStub,
) -> None:
    observed = _observe_submit(browser_stub)
    # No re-render: the re-observe would return the same (still-dead) ref, so
    # a pointless re-dispatch must be avoided.
    browser_stub.fail_selectors.add("[ref=e5]")

    result = browser_act(observed)

    assert not result.success
    assert browser_stub.clicks == ["[ref=e5]"]


def test_stale_selector_retry_uses_method_and_arguments_override(
    browser_stub: BrowserStub,
) -> None:
    observed = browser_observe("type into the search box", top_k=1)[0]
    assert observed.selector == "[ref=e2]"
    browser_stub.snapshots = [SNAPSHOT_SEARCH_MOVED]
    browser_stub.fail_selectors.add("[ref=e2]")

    result = browser_act(observed, method="fill", arguments=["rust async"])

    assert result.success
    assert result.selector_used == "[ref=e8]"
    # The stale attempt is recorded too, followed by the successful retry.
    assert browser_stub.fills == [
        ("[ref=e2]", "rust async"),
        ("[ref=e8]", "rust async"),
    ]


# ---------------------------------------------------------------------------
# P1 fix: method inferred from role, not hardcoded to "click"
# ---------------------------------------------------------------------------


def test_observe_textbox_returns_fill_method(browser_stub: BrowserStub) -> None:
    # SNAPSHOT_V1 has '- textbox "Search the news" [ref=e2]' and
    # '- textbox "Email" [ref=e7]'. Observing either must return method="fill".
    results = browser_observe("type into the search box")
    assert results, "expected at least one match"
    textbox_results = [r for r in results if r.selector in ("[ref=e2]", "[ref=e7]")]
    assert textbox_results, "expected textbox results"
    for r in textbox_results:
        assert r.method == "fill", (
            f"textbox should yield method='fill', got {r.method!r}"
        )


# ---------------------------------------------------------------------------
# P1 fix: non-ref bracket attributes are ignored; fallback uses role-name format
# ---------------------------------------------------------------------------

SNAPSHOT_NO_REFS = """\
- heading "Welcome" [level=1]
- link "Home"
- textbox "Search"
- button "Submit"
"""

# Snapshot where a ref and a second attribute appear in the same bracket.
# The selector must extract only the ref= part, not "[ref=e5, level=1]".
SNAPSHOT_MULTI_ATTR = """\
- heading "Section" [ref=e3, level=2]
- button "Submit" [ref=e5, level=1]
- link "Home"
"""


@pytest.fixture
def no_ref_browser_stub(monkeypatch: pytest.MonkeyPatch) -> BrowserStub:
    stub = BrowserStub([SNAPSHOT_NO_REFS])
    _wire_stub(stub, monkeypatch)
    return stub


def test_non_ref_bracket_ignored_uses_role_name_selector(
    no_ref_browser_stub: BrowserStub,
) -> None:
    # The heading has [level=1] — that is NOT a ref= attribute and must be
    # ignored. The fallback selector must be role=heading[name='Welcome'].
    results = browser_observe("welcome heading", top_k=5)
    heading_results = [r for r in results if "Welcome" in r.description]
    assert heading_results, "expected a match for the heading"
    r = heading_results[0]
    assert r.selector == "role=heading[name='Welcome']", (
        f"non-ref bracket should produce role-name selector, got {r.selector!r}"
    )
    assert "[level=" not in r.selector


def test_no_ref_element_uses_role_name_selector(
    no_ref_browser_stub: BrowserStub,
) -> None:
    # Elements without any bracket content also fall back to role-name format.
    results = browser_observe("home link", top_k=5)
    link_results = [r for r in results if "Home" in r.description]
    assert link_results, "expected a match for the Home link"
    assert link_results[0].selector == "role=link[name='Home']"


def test_multi_attr_bracket_extracts_only_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When a bracket contains both a ref and another attribute (e.g.
    # "[ref=e5, level=1]"), the selector must be "[ref=e5]", not the
    # invalid Playwright selector "[ref=e5, level=1]".
    stub = BrowserStub([SNAPSHOT_MULTI_ATTR])
    _wire_stub(stub, monkeypatch)
    results = browser_observe("submit button", top_k=5)
    submit = [r for r in results if "Submit" in r.description]
    assert submit, "expected a match for Submit"
    assert submit[0].selector == "[ref=e5]", (
        f"multi-attr bracket should yield only '[ref=e5]', got {submit[0].selector!r}"
    )
    # The second-attribute content must not appear in the selector.
    assert "level" not in submit[0].selector


# ---------------------------------------------------------------------------
# browser_extract
# ---------------------------------------------------------------------------


def test_extract_without_instruction_returns_raw_snapshot(
    browser_stub: BrowserStub,
) -> None:
    result = browser_extract()
    assert result.success
    assert result.data == SNAPSHOT_V1
    assert result.llm_calls == 0


def test_extract_with_instruction_is_path_a_noop_with_hint(
    browser_stub: BrowserStub,
) -> None:
    result = browser_extract("all comment counts")
    assert not result.success
    assert isinstance(result.data, dict)
    assert "error" in result.data
    assert result.llm_calls == 0


# ---------------------------------------------------------------------------
# P1 fix: snapshot failures return graceful results, not unhandled exceptions
# ---------------------------------------------------------------------------


def test_observe_snapshot_failure_returns_empty(
    failing_browser_stub: BrowserStub,
) -> None:
    # If the browser raises (closed page, PlaywrightError, etc.), observe
    # must return [] instead of propagating the exception to the caller.
    result = browser_observe("the submit button")
    assert result == []


def test_extract_snapshot_failure_returns_failed_result(
    failing_browser_stub: BrowserStub,
) -> None:
    # If the browser raises, extract must return ExtractResult(success=False)
    # instead of propagating the exception.
    result = browser_extract()
    assert not result.success
    assert isinstance(result.data, dict)
    assert "error" in result.data


# ---------------------------------------------------------------------------
# P2 fix: press focuses the element before pressing the key
# ---------------------------------------------------------------------------


def test_act_press_focuses_element_before_pressing(browser_stub: BrowserStub) -> None:
    # Verify that the press dispatch clicks (focuses) the target selector
    # before issuing the key press so the key lands on the intended element.
    observed = browser_observe("submit button", top_k=1)[0]
    result = browser_act(observed, method="press", arguments=["Enter"])
    assert result.success
    # click_element(sel) must have been called to focus the element first.
    assert observed.selector in browser_stub.clicks


# ---------------------------------------------------------------------------
# P2 fix: stale-selector retry uses original instruction, not vague description
# ---------------------------------------------------------------------------


def test_observe_result_stores_original_instruction(browser_stub: BrowserStub) -> None:
    instruction = "the submit button at the bottom of the form"
    results = browser_observe(instruction, top_k=1)
    assert results
    assert results[0].instruction == instruction


def test_stale_selector_retry_uses_original_instruction(
    browser_stub: BrowserStub,
) -> None:
    # Observe with a specific instruction, then simulate a page re-render that
    # moves the selector. The retry re-observe must use the original instruction
    # (not the vague description like "button 'Submit'") to find the element.
    observed = browser_observe("submit button", top_k=1)[0]
    assert observed.instruction == "submit button"
    assert observed.selector == "[ref=e5]"
    browser_stub.snapshots = [SNAPSHOT_SUBMIT_MOVED]
    browser_stub.fail_selectors.add("[ref=e5]")

    result = browser_act(observed)

    assert result.success
    assert result.selector_used == "[ref=e9]"


# ---------------------------------------------------------------------------
# P1 fix: non-locator failures must not trigger stale-selector retry
# (avoids double-acting on partially-executed non-idempotent actions)
# ---------------------------------------------------------------------------


def test_stale_selector_retry_skipped_on_non_locator_failure(
    browser_stub: BrowserStub,
) -> None:
    """A navigation timeout (no 'locator' in the message) must NOT be retried.

    If a click triggered navigation and then timed out, the action may have
    already executed.  Retrying would double-act (double form submission, double
    navigation).  The guard must recognise that "Timeout: navigation to '...'"
    is not a locator-not-found failure and return the first result immediately.
    """
    observed = browser_observe("submit button", top_k=1)[0]
    assert observed.selector == "[ref=e5]"

    # Page re-renders so retry *would* find a different selector — but should not.
    browser_stub.snapshots = [SNAPSHOT_SUBMIT_MOVED]
    browser_stub.custom_errors["[ref=e5]"] = TimeoutError(
        "Timeout: navigation to 'https://example.com/' exceeded 30000ms"
    )

    result = browser_act(observed)

    assert not result.success
    # Only one dispatch — no retry.
    assert browser_stub.clicks == ["[ref=e5]"]


# ---------------------------------------------------------------------------
# P1 fix: DOM-detach phrases trigger stale-selector retry
# ---------------------------------------------------------------------------


def test_stale_selector_retry_triggered_on_dom_detach(
    browser_stub: BrowserStub,
) -> None:
    """'Element is not attached to the DOM' must trigger the stale-selector retry.

    A Playwright error like 'not attached to the DOM' or 'detached from the DOM'
    means the element existed but was removed by a re-render — a classic stale
    selector.  The guard must recognise these phrases and re-observe.
    """
    observed = browser_observe("submit button", top_k=1)[0]
    assert observed.selector == "[ref=e5]"
    # Page re-renders: old ref detaches, button moves to e9.
    browser_stub.snapshots = [SNAPSHOT_SUBMIT_MOVED]
    browser_stub.custom_errors["[ref=e5]"] = RuntimeError(
        "Element is not attached to the DOM"
    )

    result = browser_act(observed)

    assert result.success
    assert result.selector_used == "[ref=e9]"
    assert browser_stub.clicks == ["[ref=e5]", "[ref=e9]"]


# ---------------------------------------------------------------------------
# P1 fix: string-form browser_act with fill element returns clear error
# ---------------------------------------------------------------------------


def test_act_string_form_fill_without_value_returns_clear_error(
    browser_stub: BrowserStub,
) -> None:
    """browser_act(str) matching a fill element must return a helpful error.

    When the instruction matches a textbox (method='fill') but no value was
    provided via arguments=, the code has no value to fill in.  It must return
    a clear message instructing the caller to use explicit arguments= instead
    of propagating an opaque AssertionError.
    """
    # "type into the search box" matches the textbox [ref=e2] with method=fill.
    result = browser_act("type into the search box")
    assert not result.success
    assert "fill" in result.message.lower()
    assert "arguments" in result.message.lower()
    # Must not have dispatched to fill_element at all (no value to fill).
    assert browser_stub.fills == []
