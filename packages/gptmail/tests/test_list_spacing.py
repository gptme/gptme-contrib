"""Tests for fix_list_spacing: list detection before Markdown rendering."""

import markdown

from gptmail.lib import fix_list_spacing


def render(src: str) -> str:
    return markdown.markdown(fix_list_spacing(src), extensions=["extra", "sane_lists"])


def test_wrapped_year_inside_bullet_is_not_a_list_item():
    # Regression: this rendered as "...11.4 in" + a new bullet "It was 10.1...",
    # silently dropping "2023".
    src = (
        "Intro.\n\n"
        "- Average day strain was 10.6 in 2022 and 11.4 in\n"
        "  2023. It was 10.1 in 2024.\n"
        "- Next bullet.\n"
    )
    html = render(src)
    assert "2023. It was 10.1" in html
    assert "<li>\n<p>" not in html  # still a tight list
    assert html.count("<li>") == 2


def test_wrapped_year_in_paragraph_is_kept():
    html = render("Strain averaged 11.4 in\n2023. Then it fell.\n")
    assert "2023. Then it fell" in html
    assert "<ol>" not in html


def test_ordered_list_after_paragraph_still_renders():
    html = render("Steps:\n1. First\n2. Second\n3. Third\n")
    assert html.count("<li>") == 3
    assert "<ol>" in html


def test_ordered_list_with_wrapped_items_keeps_numbering():
    src = "1. First item that\n   wraps onto a second line\n2. Second\n3. Third\n"
    html = render(src)
    assert html.count("<li>") == 3


def test_unordered_list_after_paragraph_gets_blank_line():
    assert fix_list_spacing("Intro\n- a\n- b") == "Intro\n\n- a\n- b"


def test_code_fence_contents_untouched():
    src = "Text\n```\nline\n2023. not touched\n```\n"
    assert fix_list_spacing(src) == src


def test_wrapped_bullets_stay_tight():
    src = "Intro.\n\n- First bullet that\n  wraps.\n- Second.\n"
    assert fix_list_spacing(src) == src
    assert "<p>" not in render(src).split("</p>", 1)[1]
