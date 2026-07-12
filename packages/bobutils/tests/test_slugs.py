"""Tests for bobutils.slugs."""

from __future__ import annotations

from bobutils.slugs import slugify


def test_slugify_basic() -> None:
    assert slugify("Hello World") == "hello-world"


def test_slugify_special_chars() -> None:
    assert slugify("Fix: some/thing!") == "fix-some-thing"


def test_slugify_numbers() -> None:
    assert slugify("v1.2.3") == "v1-2-3"


def test_slugify_strips_input() -> None:
    assert slugify("  hello  ") == "hello"


def test_slugify_max_len() -> None:
    result = slugify("a" * 100, max_len=20)
    assert len(result) <= 20


def test_slugify_max_len_no_trailing_hyphen() -> None:
    result = slugify("abc-def", max_len=6)
    assert not result.endswith("-")


def test_slugify_empty_uses_fallback() -> None:
    assert slugify("", fallback="default") == "default"


def test_slugify_empty_default_fallback() -> None:
    assert slugify("---") == "item"


def test_slugify_custom_fallback() -> None:
    assert slugify("!!!", fallback="spec") == "spec"
