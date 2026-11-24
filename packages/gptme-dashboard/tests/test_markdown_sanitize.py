"""Raw HTML in workspace markdown must not reach generated pages as live markup.

Agent journals and tasks quote outside content (issue text, PR comments,
tweets), so the markdown renderer treats raw HTML as untrusted
(gptme/gptme-contrib#2359).
"""

from pathlib import Path

from gptme_dashboard.generate import generate, render_body_html


def test_markdown_body_raw_script_is_not_emitted():
    html = render_body_html("# Notes\n\n<script>alert(document.cookie)</script>\n")
    assert "<script" not in html


def test_event_handler_attribute_is_dropped():
    html = render_body_html('Quoted:\n\n<img src="x.png" onerror="alert(1)">\n')
    assert "onerror" not in html
    assert '<img src="x.png"' in html


def test_inline_event_handler_is_dropped():
    html = render_body_html('Text <span onmouseover="alert(1)">hover</span> more\n')
    assert "onmouseover" not in html
    assert "hover" in html


def test_javascript_url_in_raw_anchor_is_dropped():
    html = render_body_html('<a href="javascript:alert(1)">click</a>\n')
    assert "javascript:" not in html
    assert "click" in html


def test_iframe_and_style_tags_are_dropped():
    html = render_body_html(
        '<iframe src="https://evil.example"></iframe>\n\n<style>body{display:none}</style>\n'
    )
    assert "<iframe" not in html
    assert "<style" not in html


def test_safe_raw_html_survives():
    body = (
        "<details>\n<summary>More</summary>\n\nHidden **text**.\n\n</details>\n\n"
        'Line<br>break and <kbd>Ctrl</kbd>.\n\n<p align="center">centered</p>\n'
    )
    html = render_body_html(body)
    assert "<details>" in html and "</details>" in html
    assert "<summary>More</summary>" in html
    assert "<strong>text</strong>" in html
    assert "<br>" in html
    assert "<kbd>Ctrl</kbd>" in html
    assert 'align="center"' in html


def test_highlighted_code_keeps_inline_styles():
    html = render_body_html("```python\ndef f():\n    return 1\n```\n")
    assert 'style="color:' in html.replace(": ", ":")
    assert "def" in html


def test_heading_anchor_ids_and_table_alignment_survive():
    html = render_body_html("## Some Heading\n\n| a | b |\n|:-|-:|\n| 1 | 2 |\n")
    assert 'id="some-heading"' in html
    assert "text-align:right" in html.replace(": ", ":")


def test_generated_journal_page_does_not_contain_raw_handler(tmp_path: Path):
    p = tmp_path / "journal/2026-11-24/session.md"
    p.parent.mkdir(parents=True)
    p.write_text("# Session\n\nQuoted issue text:\n\n<img src=x onerror=alert(1)>\n")
    generate(tmp_path, tmp_path / "_site")
    page = (tmp_path / "_site/journal/2026-11-24/session.html").read_text()
    assert "onerror=alert(1)" not in page
    assert "onerror" not in page
