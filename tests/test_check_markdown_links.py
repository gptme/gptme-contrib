"""Tests for the markdown link checker pre-commit hook."""

import importlib.util
from pathlib import Path

# Import from non-package script path
_spec = importlib.util.spec_from_file_location(
    "check_markdown_links",
    Path(__file__).parent.parent / "scripts" / "precommit" / "check_markdown_links.py",
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
extract_links = _mod.extract_links
strip_code = _mod.strip_code


def test_extract_links_basic():
    content = "[text](file.md)"
    links = extract_links(content)
    assert links == [("text", "file.md")]


def test_extract_links_multiple():
    content = "[a](one.md) and [b](two.md)"
    links = extract_links(content)
    assert links == [("a", "one.md"), ("b", "two.md")]


def test_extract_links_skips_fenced_code_blocks():
    content = """\
# Real content

[real](exists.md)

```markdown
[example](nonexistent.md)
```

More text.
"""
    links = extract_links(content)
    assert links == [("real", "exists.md")]


def test_extract_links_skips_tilde_fenced_blocks():
    content = """\
[real](exists.md)

~~~markdown
[example](nonexistent.md)
~~~
"""
    links = extract_links(content)
    assert links == [("real", "exists.md")]


def test_extract_links_skips_inline_code():
    content = """\
[real](exists.md)

`[inline](nonexistent.md)`
"""
    links = extract_links(content)
    assert links == [("real", "exists.md")]


def test_extract_links_skips_nested_code_in_fenced_block():
    """Links inside fenced blocks are skipped even with other markdown."""
    content = """\
[before](a.md)

```markdown
# Example Index

## Data
- [file-a.md](file-a.md) — description
- [file-b.md](file-b.md) — description
```

[after](b.md)
"""
    links = extract_links(content)
    assert links == [("before", "a.md"), ("after", "b.md")]


def test_strip_code_preserves_non_code():
    content = "# Hello\n\n[link](file.md)\n"
    assert "[link](file.md)" in strip_code(content)


def test_strip_code_removes_fenced_block():
    content = """\
before

```
inside
```

after
"""
    stripped = strip_code(content)
    assert "inside" not in stripped
    assert "before" in stripped
    assert "after" in stripped


def test_strip_code_removes_inline_code():
    content = "text `code here` more text"
    stripped = strip_code(content)
    assert "code here" not in stripped
    assert "text" in stripped
    assert "more text" in stripped


def test_extract_links_mixed_real_and_code():
    """The exact pattern that caused CI failure in gptme-contrib PR #651."""
    content = """\
# Indexed Knowledge Base

[real-link](../workflow/git-workflow.md)

```markdown
# Knowledge Index

## Data & Analysis
- [qs-data-landscape.md](qs-data-landscape.md) — description
- [predictive-framework.md](predictive-framework.md) — description

## Feedback
- [feedback-journal-format.md](feedback-journal-format.md) — description
```

[another-real](../tools/some-tool.md)
"""
    links = extract_links(content)
    link_targets = [target for _, target in links]
    assert "../workflow/git-workflow.md" in link_targets
    assert "../tools/some-tool.md" in link_targets
    assert "qs-data-landscape.md" not in link_targets
    assert "predictive-framework.md" not in link_targets
    assert "feedback-journal-format.md" not in link_targets
