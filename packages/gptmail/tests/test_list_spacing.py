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


def test_other_fence_marker_inside_code_block_is_code():
    # A "~~~" line inside a ``` block must not close it.
    src = "Text\n```\n~~~\n2023. not touched\n```\n"
    assert fix_list_spacing(src) == src
    src = "~~~~\n~~~\n2023. not touched\n~~~~\n"
    assert fix_list_spacing(src) == src


def test_text_after_code_block_is_processed():
    src = "```\ncode\n```\nStrain was 11.4 in\n2023. Then it fell.\n"
    assert "2023. Then it fell" in render(src)


def test_wrapped_year_in_paragraph_after_ordered_list_is_kept():
    # The ordered list ended at the blank line; the paragraph's wrapped year
    # is not item 2023.
    for indent in ("", "   ", "    "):
        src = f"1. One\n2. Two\n\n{indent}Strain was 11.4 in\n{indent}2023. Then it fell.\n"
        html = render(src)
        assert "2023. Then it fell" in html, indent
        assert 'start="2023"' not in html, indent


def test_lazy_continuation_keeps_numbering():
    html = render("1. First item\nlazily wrapped\n2. Second\n")
    assert html.count("<li>") == 2


def test_no_number_is_dropped_after_loose_item_paragraph():
    # Ambiguous shape (no blank before "2."): it may render as text rather
    # than a new item, but the number must survive.
    html = render("1. a\n\n    para\n2. b\n")
    assert "2. b" in html or 'start="2"' in html or html.count("<li>") == 2


def test_sub_bullet_inside_ordered_item_keeps_numbering():
    # An indented sub-bullet must not end the ordered context and get "2." escaped.
    for indent in ("   ", "    "):
        src = f"1. First\n{indent}- sub\n2. Second\n"
        assert "2\\." not in fix_list_spacing(src)
        html = render(src)
        assert html.count("<ol>") == 1
        assert "<li>Second</li>" in html


def test_top_level_bullet_ends_ordered_context():
    out = fix_list_spacing("1. One\n- Bullet\n2023. It was a good year.\n")
    assert "2023\\. It was" in out


def test_wrapped_year_after_code_block_following_list_is_kept():
    # An unindented fence ends the ordered list; the year after it is prose.
    src = "1. One\n2. Two\n```\ncode\n```\nStrain was 11.4 in\n2023. Then it fell.\n"
    html = render(src)
    assert "2023. Then it fell" in html


def test_fence_inside_ordered_item_keeps_numbering():
    src = "1. First\n   ```\n   code\n   ```\n2. Second\n"
    assert "2\\." not in fix_list_spacing(src)
