"""Token file contract: light/dark parity, manual override and contrast."""

from __future__ import annotations

import re

import pytest
from gptme_vitals.theme import (
    BASE_CSS,
    THEME_INIT_JS,
    TOKENS_CSS,
    render_head_assets,
    render_theme_toggle,
)


def _vars(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))


def _blocks() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    dark = re.search(r"^:root \{(.*?)\n\}", TOKENS_CSS, re.S | re.M)
    pinned = re.search(r':root\[data-theme="light"\] \{(.*?)\n\}', TOKENS_CSS, re.S)
    media = re.search(
        r':root:not\(\[data-theme="dark"\]\) \{(.*?)\n  \}', TOKENS_CSS, re.S
    )
    assert dark and pinned and media
    return _vars(dark.group(1)), _vars(pinned.group(1)), _vars(media.group(1))


def _lum(hex_colour: str) -> float:
    h = hex_colour.strip().lstrip("#")
    chans = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_token_has_a_light_value_and_the_two_light_blocks_match() -> None:
    dark, pinned, media = _blocks()
    assert set(pinned) == set(dark), "light must redefine every dark token"
    assert pinned == media, "manual and prefers-color-scheme light blocks drifted"


def test_light_follows_os_unless_dark_is_pinned() -> None:
    assert "@media (prefers-color-scheme: light)" in TOKENS_CSS
    # A pinned dark theme must beat the OS preference, and a pinned light one
    # must work without it.
    assert ':root:not([data-theme="dark"])' in TOKENS_CSS
    assert ':root[data-theme="light"]' in TOKENS_CSS


@pytest.mark.parametrize("scheme", [0, 1])
def test_text_tokens_meet_aa_on_every_surface(scheme: int) -> None:
    tokens = _blocks()[scheme]
    for fg in (
        "--text",
        "--accent",
        "--green",
        "--yellow",
        "--red",
        "--dim",
        "--chart-blue",
    ):
        for bg in ("--bg", "--card-bg", "--tile-bg"):
            ratio = _contrast(tokens[fg], tokens[bg])
            assert ratio >= 4.5, f"{fg} on {bg}: {ratio:.2f}"


@pytest.mark.parametrize("scheme", [0, 1])
def test_badges_meet_aa(scheme: int) -> None:
    tokens = _blocks()[scheme]
    pairs = {
        "--badge-ok-bg": "--dim",
        "--badge-green-bg": "--green",
        "--badge-yellow-bg": "--yellow",
        "--badge-red-bg": "--red",
    }
    for bg, fg in pairs.items():
        assert _contrast(tokens[fg], tokens[bg]) >= 4.5, (bg, fg)


def test_base_css_uses_only_declared_tokens_and_no_hardcoded_palette() -> None:
    declared = set(_blocks()[0]) | {"--stat-status"}
    used = set(re.findall(r"var\((--[\w-]+)", BASE_CSS))
    assert used <= declared, used - declared
    assert not re.search(
        r"#[0-9a-fA-F]{3,6}\b", BASE_CSS
    ), "hardcoded colour in BASE_CSS"


def test_head_assets_and_toggle() -> None:
    head = render_head_assets()
    assert TOKENS_CSS in head and BASE_CSS in head and THEME_INIT_JS in head
    assert head.index("<style>") < head.index("<script>")
    toggle = render_theme_toggle()
    assert 'class="theme-toggle"' in toggle
    # The visible text carries the current mode; a fixed aria-label would hide it
    # from screen-reader users.
    assert "aria-label" not in toggle
    assert "localStorage" in toggle
    # Storage access is guarded: a throwing localStorage must not break the page.
    assert "try{" in THEME_INIT_JS and "catch(e)" in THEME_INIT_JS
