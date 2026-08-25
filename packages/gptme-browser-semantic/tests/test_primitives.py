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

# Realistic gptme snapshot_page() shape: page header, level attributes, no refs.
SNAPSHOT_GPTME = """\
Page: Hacker News (fixture)
URL: file:///tmp/hn.html

- heading "Hacker News" [level=1]
- link "new"
- link "past"
- button "more"
- textbox "Search"
- button "Search"
- link "First story"
- link "42 comments"
- textbox "Write a comment"
- button "Submit"
"""


class BrowserStub:
    """Recording stand-in for ``gptme.tools.browser``.

    ``snapshots`` is a queue: every ``snapshot_page()`` call pops the next
    snapshot, and the last one sticks once the queue is exhausted (models a
    stable page).
    """

    def __init__(self, snapshots: list[str]) -> None:
        self.snapshots = snapshots
        self.snapshot_calls = 0
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.fail_selectors: set[str] = set()

    def snapshot_page(self) -> str:
        self.snapshot_calls += 1
        idx = min(self.snapshot_calls - 1, len(self.snapshots) - 1)
        return self.snapshots[idx]

    def click_element(self, selector: str) -> None:
        # Record the attempt *before* the failure check: tests assert on the
        # dispatch sequence, including the stale attempt that raised.
        self.clicks.append(selector)
        if selector in self.fail_selectors:
            raise TimeoutError(f"locator '{selector}' not found")

    def fill_element(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))
        if selector in self.fail_selectors:
            raise TimeoutError(f"locator '{selector}' not found")

    def press_key(self, key: str) -> None:  # pragma: no cover - passthrough
        pass

    def select_option(self, selector: str, value: str) -> None:  # pragma: no cover
        pass

    def hover_element(self, selector: str) -> None:  # pragma: no cover - passthrough
        pass


@pytest.fixture
def browser_stub(monkeypatch: pytest.MonkeyPatch) -> BrowserStub:
    stub = BrowserStub([SNAPSHOT_V1])
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
    return stub


# ---------------------------------------------------------------------------
# browser_observe — ranking
# ---------------------------------------------------------------------------


def test_observe_ranks_best_match_first(browser_stub: BrowserStub) -> None:
    results = browser_observe("type into the search box")
    assert results, "expected at least one match"
    assert results[0].selector == "[ref=e2]"
    assert results[0].method == "fill"


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
# parser: ignore non-ref bracket attrs; role fallback when gptme has no refs
# ---------------------------------------------------------------------------


def test_parser_ignores_level_attribute_and_uses_role_selector(
    browser_stub: BrowserStub,
) -> None:
    browser_stub.snapshots = [SNAPSHOT_GPTME]
    results = browser_observe("the first story link", top_k=1)
    assert results[0].selector == "role=link[name='First story']"
    # Must not have treated [level=1] on the heading as a selector.
    heading = browser_observe("Hacker News", top_k=1)
    assert heading[0].selector == "role=heading[name='Hacker News']"
    assert "level" not in heading[0].selector


def test_observe_without_refs_uses_role_name_locator(
    browser_stub: BrowserStub,
) -> None:
    browser_stub.snapshots = [SNAPSHOT_GPTME]
    results = browser_observe("type into the search box", top_k=1)
    assert results[0].selector == "role=textbox[name='Search']"
    assert results[0].method == "fill"


# ---------------------------------------------------------------------------
# browser_act — string form basics
# ---------------------------------------------------------------------------


def test_act_string_form_observes_then_dispatches(browser_stub: BrowserStub) -> None:
    result = browser_act("open the first story")
    assert result.success
    assert result.selector_used == "[ref=e4]"
    assert browser_stub.clicks == ["[ref=e4]"]


def test_act_no_match_fails_without_dispatch(browser_stub: BrowserStub) -> None:
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


def test_act_fill_without_arguments_fails(browser_stub: BrowserStub) -> None:
    result = browser_act("type into the search box", method="fill", arguments=[])
    assert not result.success
    assert "fill requires arguments" in result.message
    assert browser_stub.fills == []


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
# browser_extract
# ---------------------------------------------------------------------------


def test_extract_without_instruction_returns_raw_snapshot(
    browser_stub: BrowserStub,
) -> None:
    result = browser_extract()
    assert result.success
    assert result.data == SNAPSHOT_V1
    assert result.llm_calls == 0


def test_extract_with_instruction_still_returns_raw_aria(
    browser_stub: BrowserStub,
) -> None:
    # Path A does not interpret the instruction; it returns the snapshot.
    result = browser_extract("all comment counts")
    assert result.success
    assert result.data == SNAPSHOT_V1
    assert result.llm_calls == 0


def test_extract_with_schema_is_path_b_stub(browser_stub: BrowserStub) -> None:
    result = browser_extract("all comment counts", schema=dict)
    assert not result.success
    assert isinstance(result.data, dict)
    assert "error" in result.data
    assert result.llm_calls == 0
