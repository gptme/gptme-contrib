"""Tests for gptme_rag.lesson_matcher (Phase 2.4 upstream)."""

from __future__ import annotations

import math
import sys
from pathlib import Path


from gptme_rag.lesson_matcher import (
    BM25_MIN_Z,
    _extract_list_frontmatter_field,
    BM25_STANDOUT_FRACTION,
    extract_frontmatter,
    filter_by_harness,
    filter_by_session_category,
    filter_held_out,
    is_held_out,
    keyword_to_regex,
    match_keyword,
    parse_holdout_set,
    scan_lessons,
    score_lessons,
    _bm25_min_z,
    _bm25_score,
    _bm25_zscores,
    _build_bm25_index,
)


# ---------------------------------------------------------------------------
# keyword_to_regex
# ---------------------------------------------------------------------------


class TestKeywordToRegex:
    def test_plain_keyword(self):
        pat = keyword_to_regex("merge conflict")
        assert pat is not None
        assert pat.search("you have a merge conflict here")

    def test_wildcard_suffix(self):
        pat = keyword_to_regex("git*")
        assert pat is not None
        assert pat.search("running git status")
        assert pat.search("gitignore")

    def test_wildcard_mid(self):
        # * → \w* (word chars only, no hyphen)
        # So pre*commit matches "precommit" but NOT "pre-commit"
        pat = keyword_to_regex("pre*commit")
        assert pat is not None
        assert pat.search("precommit config")
        # Hyphenated form does NOT match (wildcard is \w*, not [\w-]*)
        assert not pat.search("pre-commit hook")

    def test_bare_wildcard_returns_none(self):
        assert keyword_to_regex("*") is None

    def test_wildcard_only_keyword_returns_none(self):
        assert keyword_to_regex("**") is None
        assert keyword_to_regex(" *** ") is None

    def test_empty_returns_none(self):
        assert keyword_to_regex("") is None
        assert keyword_to_regex("   ") is None

    def test_case_insensitive(self):
        pat = keyword_to_regex("MERGE")
        assert pat is not None
        assert pat.search("merge conflict")

    def test_special_chars_escaped(self):
        pat = keyword_to_regex("foo.bar")
        assert pat is not None
        # Literal dot — should NOT match "fooXbar"
        assert not pat.search("fooXbar")
        assert pat.search("foo.bar in config")


# ---------------------------------------------------------------------------
# match_keyword
# ---------------------------------------------------------------------------


class TestMatchKeyword:
    def test_basic(self):
        assert match_keyword("merge conflict", "you have a merge conflict")
        assert not match_keyword("merge conflict", "no problem here")

    def test_wildcard(self):
        assert match_keyword("git*", "running git status")
        assert match_keyword("git*", "gitignore file")

    def test_bare_wildcard_never_matches(self):
        # keyword_to_regex("*") returns None → match_keyword returns False
        assert not match_keyword("*", "anything at all")

    def test_empty_keyword(self):
        assert not match_keyword("", "some text")


# ---------------------------------------------------------------------------
# extract_frontmatter
# ---------------------------------------------------------------------------


class TestExtractFrontmatter:
    def test_yaml(self):
        content = "---\nstatus: active\ndescription: test\n---\n# Body\n"
        fm, body = extract_frontmatter(content)
        assert fm.get("status") == "active"
        assert fm.get("description") == "test"
        assert "Body" in body

    def test_no_frontmatter(self):
        content = "# Just a markdown file\nNo frontmatter."
        fm, body = extract_frontmatter(content)
        assert fm == {}
        assert "Just a markdown file" in body

    def test_yaml_sequence_returns_empty_dict(self):
        # If the top-level YAML is a list rather than a mapping, extract_frontmatter
        # must return {} (not the list) so callers can safely call .get() on it.
        content = "---\n- item_a\n- item_b\n---\n# Body\n"
        fm, body = extract_frontmatter(content)
        assert fm == {}, f"Expected empty dict, got {type(fm).__name__}: {fm!r}"
        assert isinstance(fm, dict)
        assert "Body" in body

    def test_incomplete_delimiter(self):
        content = "---\nstatus: active\n"  # no closing ---
        fm, body = extract_frontmatter(content)
        assert fm == {}

    def test_match_keywords_nested(self):
        content = (
            "---\nmatch:\n  keywords:\n    - merge conflict\n    - rebase\n"
            "status: active\n---\n# Rule\nBody text.\n"
        )
        fm, body = extract_frontmatter(content)
        assert fm.get("match", {}).get("keywords") == ["merge conflict", "rebase"]

    def test_regex_fallback_without_yaml(self, monkeypatch):
        # Simulate yaml unavailable by making extract_frontmatter use the fallback
        content = (
            '---\nmatch:\n  keywords: ["git push", "git commit"]\n'
            "status: active\n---\n# Title\nBody.\n"
        )
        # Directly test: without PyYAML the inline-list regex parses keywords
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, body = extract_frontmatter(content)
        # The regex fallback picks up inline keyword list
        kw = fm.get("match", {}).get("keywords", fm.get("keywords", []))
        assert "git push" in kw or "git commit" in kw

    def test_regex_fallback_strips_inline_comment(self, monkeypatch):
        # The canonical lesson template in gptme-contrib/lessons/README.md writes
        # `status: active  # active | automated | ...`, so a fallback that keeps
        # the comment yields status != "active" and scan_lessons drops the lesson.
        content = (
            "---\n"
            "status: active  # active | automated | deprecated\n"
            "name: my-skill   # the trigger name\n"
            'description: "hash # inside quotes stays"\n'
            "---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("status") == "active"
        assert fm.get("name") == "my-skill"
        # A `#` inside a quoted scalar is data, not a comment.
        assert fm.get("description") == "hash # inside quotes stays"

    def test_regex_fallback_processes_double_quoted_escapes(self, monkeypatch):
        # Per YAML spec, double-quoted scalars process backslash escapes:
        # `"He said \"hello\""` → `He said "hello"` (not the raw `He said \"hello\"`).
        # The old code found the closing quote correctly but returned raw value[1:index]
        # without substituting escape sequences, silently corrupting descriptions.
        content = '---\nstatus: active\ndescription: "He said \\"hello\\""\n---\n# Title\nBody.\n'
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("description") == 'He said "hello"'

    def test_regex_fallback_hash_without_leading_space_is_not_a_comment(self, monkeypatch):
        # YAML only starts a comment at a `#` that begins the value or follows
        # whitespace, so `issue#42` must survive intact.
        content = "---\nname: issue#42\n---\n# Title\nBody.\n"
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("name") == "issue#42"

    def test_regex_fallback_comment_at_parent_indent(self, monkeypatch):
        content = (
            "---\nmatch:\n"
            "# A same-indent comment remains part of the mapping.\n"
            "  keywords: [merge conflict]\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("match", {}).get("keywords") == ["merge conflict"]

    def test_regex_fallback_combines_top_level_and_nested_match_lists(self, monkeypatch):
        content = (
            "---\nkeywords: [legacy]\npatterns: [legacy-pattern]\n"
            "match:\n  keywords: [nested]\n  patterns: [nested-pattern]\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm["match"]["keywords"] == ["legacy", "nested"]
        assert fm["match"]["patterns"] == ["legacy-pattern", "nested-pattern"]

    def test_regex_fallback_scalar_keywords_and_patterns(self, monkeypatch):
        content = (
            "---\nkeywords: legacy one, legacy two\npatterns: legacy-a, legacy-b\n"
            "match:\n  keywords: nested one, nested two\n"
            "  patterns: nested-a, nested-b\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm["match"]["keywords"] == [
            "legacy one",
            "legacy two",
            "nested one",
            "nested two",
        ]
        assert fm["match"]["patterns"] == [
            "legacy-a",
            "legacy-b",
            "nested-a",
            "nested-b",
        ]

    def test_regex_fallback_session_categories_block(self, monkeypatch):
        # Regression: without PyYAML, session_categories was not parsed,
        # causing gated lessons to fire in every session (security defect).
        content = (
            "---\nmatch:\n  keywords:\n    - foo\n"
            "  session_categories:\n    - code\n    - infrastructure\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, body = extract_frontmatter(content)
        match = fm.get("match", {})
        assert match.get("keywords") == ["foo"]
        assert match.get("session_categories") == ["code", "infrastructure"]

    def test_regex_fallback_list_fields_stop_at_sibling_key(self, monkeypatch):
        content = (
            "---\nmatch:\n  session_categories:\n    - code\n"
            "  keywords:\n    - multi word keyword\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, body = extract_frontmatter(content)
        match = fm.get("match", {})
        assert match.get("session_categories") == ["code"]
        assert match.get("keywords") == ["multi word keyword"]

    def test_regex_fallback_session_categories_inline(self, monkeypatch):
        content = (
            "---\nmatch:\n  keywords:\n    - foo\n"
            "  session_categories: [code, infrastructure]\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, body = extract_frontmatter(content)
        cats = fm.get("match", {}).get("session_categories", [])
        assert "code" in cats
        assert "infrastructure" in cats

    def test_regex_fallback_ignores_commented_inline_lists(self, monkeypatch):
        content = (
            "---\nmatch:\n"
            "  # session_categories: [social]\n"
            "  # keywords: [commented keyword]\n"
            "  keywords: [active keyword]\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        match = fm.get("match", {})
        assert match.get("keywords") == ["active keyword"]
        assert "session_categories" not in match

    def test_regex_fallback_metadata_harness(self, monkeypatch):
        content = (
            "---\nmatch:\n  keywords:\n    - foo\n"
            "metadata:\n  harness:\n    - claude-code\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, body = extract_frontmatter(content)
        harness = fm.get("metadata", {}).get("harness", [])
        assert "claude-code" in harness

    def test_regex_fallback_id_and_metadata_tags(self, monkeypatch):
        content = (
            "---\nid: lesson-id # stable holdout identifier\n"
            "name: deploy-skill\n"
            "metadata:\n  tags:\n    - deployment\n    - 'release automation' # comment\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("id") == "lesson-id"
        assert fm.get("metadata", {}).get("tags") == ["deployment", "release automation"]

    def test_regex_fallback_metadata_tags_scalar(self, monkeypatch):
        content = (
            "---\nname: deploy-skill\nmetadata:\n  tags: deployment, release\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("metadata", {}).get("tags") == ["deployment", "release"]

    def test_regex_fallback_ignores_top_level_harness(self, monkeypatch):
        content = (
            "---\nharness: gptme\nmatch:\n  keywords:\n    - foo\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert "harness" not in fm.get("metadata", {})

    def test_regex_fallback_session_category_scalar_strips_comment(self, monkeypatch):
        content = (
            '---\nmatch:\n  session_categories: "code" # comment\n'
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("match", {}).get("session_categories") == ["code"]

    def test_regex_fallback_unquoted_block_keywords(self, monkeypatch):
        # Regression: without PyYAML, block-form keywords without quotes were
        # silently dropped because the regex only matched "quoted" values.
        # Real lesson files use unquoted multi-word keywords like:
        #   - unclosed code block
        #   - merge conflict
        content = (
            "---\nmatch:\n  keywords:\n"
            "    - unclosed code block\n"
            "    - merge conflict\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, body = extract_frontmatter(content)
        kw = fm.get("match", {}).get("keywords", [])
        assert "unclosed code block" in kw
        assert "merge conflict" in kw

    def test_regex_fallback_ignores_top_level_session_categories(self, monkeypatch):
        # Regression: the fallback regex matched session_categories at ANY
        # indentation, so an undocumented top-level key was hoisted into
        # match.session_categories and started gating a lesson that the PyYAML
        # path treats as unrestricted. The two parsers must agree.
        content = (
            "---\nsession_categories: code\n"
            "match:\n  keywords:\n    - foo\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert "session_categories" not in fm.get("match", {})
        assert fm.get("match", {}).get("keywords") == ["foo"]

    def test_regex_fallback_session_categories_scalar(self, monkeypatch):
        # Regression: without PyYAML, scalar session_categories like
        # "session_categories: code, infrastructure" were not parsed by
        # _extract_list_frontmatter_field (only handles list/block forms),
        # causing the lesson to fire in every session (security defect).
        content = (
            "---\nmatch:\n  keywords:\n    - foo\n"
            "  session_categories: code, infrastructure\n"
            "status: active\n---\n# Title\nBody.\n"
        )
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, body = extract_frontmatter(content)
        cats = fm.get("match", {}).get("session_categories", [])
        assert "code" in cats
        assert "infrastructure" in cats

    def test_regex_fallback_session_categories_quoted_scalar(self, monkeypatch):
        # Regression: scalar session_categories with surrounding quotes like
        # `session_categories: "code"` were captured *including* the quotes,
        # causing filter_by_session_category to compare '"code"' against 'code'
        # and silently drop the gate (security defect — gated lesson fires in
        # every session instead of only the intended ones).
        content = '---\nmatch:\n  session_categories: "code"\nstatus: active\n---\n# Title\nBody.\n'
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, body = extract_frontmatter(content)
        cats = fm.get("match", {}).get("session_categories", [])
        assert cats == ["code"], f'expected ["code"] but got {cats!r}'

    def test_regex_fallback_single_quoted_doubled_escape(self, monkeypatch):
        # Regression: YAML single-quoted scalars use '' (doubled single quote)
        # to embed a literal single quote. The old code terminated the string at
        # the first '' and returned only the prefix (e.g. 'it''s' → 'it' instead
        # of the correct "it's"), silently corrupting lesson names/descriptions.
        content = "---\nstatus: active\nname: 'it''s a lesson'\n---\n# Title\nBody.\n"
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("name") == "it's a lesson"

    def test_regex_fallback_double_quoted_backslash_escape(self, monkeypatch):
        # Regression: double-quoted YAML scalars use backslash escape sequences.
        # The old code found the closing quote correctly (treating \" as escaped)
        # but returned the raw value[1:index] without processing the escapes, so
        # `description: "He said \"hello\""` → 'He said \\"hello\\"' instead of
        # 'He said "hello"', corrupting BM25 indexing and skill-name matching.
        content = '---\nstatus: active\nname: "He said \\"hello\\""\n---\n# Body.\n'
        import builtins

        real_import = builtins.__import__

        def block_yaml(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("yaml blocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        fm, _ = extract_frontmatter(content)
        assert fm.get("name") == 'He said "hello"'


# ---------------------------------------------------------------------------
# scan_lessons
# ---------------------------------------------------------------------------


def _write_lesson(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _basic_lesson(keywords: list[str], status: str = "active") -> str:
    kw_yaml = "\n".join(f'    - "{k}"' for k in keywords)
    return f"---\nmatch:\n  keywords:\n{kw_yaml}\nstatus: {status}\n---\n# Rule\nBody text.\n"


class TestExtractListFrontmatterField:
    """Direct unit tests for the PyYAML-free list parser."""

    def test_inline_unquoted_multiword_values_split_on_commas(self):
        # Tokenizing on word characters would yield ["git", "push", ...].
        assert _extract_list_frontmatter_field(
            "keywords: [git push, git commit]\n", "keywords"
        ) == ["git push", "git commit"]

    def test_inline_mixed_quoting(self):
        assert _extract_list_frontmatter_field("keywords: ['a b', c d, \"e f\"]\n", "keywords") == [
            "a b",
            "c d",
            "e f",
        ]

    def test_block_items_flush_with_their_key(self):
        # Valid YAML: sequence items need not be indented past the key.
        assert _extract_list_frontmatter_field(
            "keywords:\n- foo bar\n- baz\nstatus: active\n", "keywords"
        ) == ["foo bar", "baz"]

    def test_unquoted_block_item_strips_inline_comment(self):
        assert _extract_list_frontmatter_field(
            "keywords:\n  - merge conflict # important\n", "keywords"
        ) == ["merge conflict"]

    def test_quoted_block_item_preserves_hash(self):
        assert _extract_list_frontmatter_field(
            'keywords:\n  - "merge # conflict" # important\n', "keywords"
        ) == ["merge # conflict"]

    def test_block_skips_interspersed_comment(self):
        assert _extract_list_frontmatter_field(
            "session_categories:\n"
            "  - code\n"
            "  # Applies to interactive implementation sessions too.\n"
            "  - interactive\n",
            "session_categories",
        ) == ["code", "interactive"]

    def test_block_stops_at_sibling_key_under_same_parent(self):
        fm = "match:\n  session_categories:\n    - code\n  keywords:\n    - foo\n"
        assert _extract_list_frontmatter_field(fm, "session_categories") == ["code"]
        assert _extract_list_frontmatter_field(fm, "keywords") == ["foo"]

    def test_absent_field_returns_empty(self):
        assert _extract_list_frontmatter_field("status: active\n", "keywords") == []

    def test_inline_quoted_commas_not_split(self):
        # Regression: naive split(",") turned ["git, push", "rebase"] into
        # ['"git', 'push"', 'rebase'] — commas inside double quotes must not
        # be treated as item separators.
        assert _extract_list_frontmatter_field(
            'keywords: ["git, push", "rebase"]\n', "keywords"
        ) == ["git, push", "rebase"]

    def test_inline_quoted_closing_bracket_is_data(self):
        assert _extract_list_frontmatter_field('keywords: ["a]b", "c"]\n', "keywords") == [
            "a]b",
            "c",
        ]


class TestScanLessons:
    def test_empty_dir(self, tmp_path):
        lessons = scan_lessons([tmp_path])
        assert lessons == []

    def test_nonexistent_dir(self, tmp_path):
        lessons = scan_lessons([tmp_path / "missing"])
        assert lessons == []

    def test_basic(self, tmp_path):
        _write_lesson(tmp_path / "foo.md", _basic_lesson(["merge conflict"]))
        lessons = scan_lessons([tmp_path])
        assert len(lessons) == 1
        assert lessons[0]["keywords"] == ["merge conflict"]

    def test_readme_skipped(self, tmp_path):
        _write_lesson(tmp_path / "README.md", _basic_lesson(["foo"]))
        lessons = scan_lessons([tmp_path])
        assert lessons == []

    def test_inactive_skipped(self, tmp_path):
        _write_lesson(tmp_path / "foo.md", _basic_lesson(["foo"], status="deprecated"))
        lessons = scan_lessons([tmp_path])
        assert lessons == []

    def test_quoted_inactive_skipped_without_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "yaml", None)
        content = _basic_lesson(["foo"], status='"deprecated"')
        _write_lesson(tmp_path / "foo.md", content)
        assert scan_lessons([tmp_path]) == []

    def test_no_keywords_skipped(self, tmp_path):
        content = "---\nstatus: active\n---\n# Title\nBody.\n"
        _write_lesson(tmp_path / "foo.md", content)
        lessons = scan_lessons([tmp_path])
        assert lessons == []

    def test_first_dir_wins(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        _write_lesson(dir_a / "foo.md", _basic_lesson(["from-a"]))
        _write_lesson(dir_b / "foo.md", _basic_lesson(["from-b"]))
        lessons = scan_lessons([dir_a, dir_b])
        assert len(lessons) == 1
        assert "from-a" in lessons[0]["keywords"]

    def test_same_filename_in_different_subdirectories_first_wins(self, tmp_path):
        # Filename dedup applies within a root too (lexicographic order: db < git).
        # Lesson filenames are treated as identities: the live corpus has 30
        # duplicate basenames and every one is the *same* lesson in two places
        # (archived/ vs category/, or local vs contrib), never two distinct lessons.
        _write_lesson(tmp_path / "git" / "merge.md", _basic_lesson(["git-merge"]))
        _write_lesson(tmp_path / "db" / "merge.md", _basic_lesson(["db-merge"]))
        lessons = scan_lessons([tmp_path])
        assert [lesson["keywords"] for lesson in lessons] == [["db-merge"]]

    def test_matching_relative_path_first_dir_wins(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        _write_lesson(dir_a / "git" / "merge.md", _basic_lesson(["from-a"]))
        _write_lesson(dir_b / "git" / "merge.md", _basic_lesson(["from-b"]))
        lessons = scan_lessons([dir_a, dir_b])
        assert len(lessons) == 1
        assert lessons[0]["keywords"] == ["from-a"]

    def test_same_filename_different_category_first_dir_wins(self, tmp_path):
        # Dedup is by filename, not relative path: a local copy that was moved
        # to another category dir (e.g. lessons/archived/foo.md, still active)
        # must still override contrib's lessons/patterns/foo.md. The live corpus
        # has real cases of this and none of distinct same-named lessons.
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        _write_lesson(dir_a / "archived" / "foo.md", _basic_lesson(["from-a"]))
        _write_lesson(dir_b / "patterns" / "foo.md", _basic_lesson(["from-b"]))
        lessons = scan_lessons([dir_a, dir_b])
        assert [lesson["keywords"] for lesson in lessons] == [["from-a"]]

    def test_session_categories(self, tmp_path):
        content = (
            "---\nmatch:\n  keywords:\n    - foo\n"
            "  session_categories:\n    - code\n"
            "status: active\n---\n# Title\n"
        )
        _write_lesson(tmp_path / "foo.md", content)
        lessons = scan_lessons([tmp_path])
        assert lessons[0]["session_categories"] == ["code"]

    def test_session_categories_comma_separated_string(self, tmp_path):
        # Regression: YAML scalar "code, infrastructure" was wrapped in a list as
        # ["code, infrastructure"] instead of being split into ["code", "infrastructure"].
        # Verifies that filter_by_session_category("code") works correctly.
        content = (
            "---\nmatch:\n  keywords:\n    - foo\n"
            "  session_categories: code, infrastructure\n"
            "status: active\n---\n# Title\n"
        )
        _write_lesson(tmp_path / "foo.md", content)
        lessons = scan_lessons([tmp_path])
        assert lessons[0]["session_categories"] == ["code", "infrastructure"]

    def test_title_from_h1(self, tmp_path):
        content = (
            "---\nmatch:\n  keywords:\n    - foo\nstatus: active\n---\n# My Lesson Title\nBody.\n"
        )
        _write_lesson(tmp_path / "my-lesson.md", content)
        lessons = scan_lessons([tmp_path])
        assert lessons[0]["title"] == "My Lesson Title"

    def test_title_fallback_to_stem(self, tmp_path):
        content = "---\nmatch:\n  keywords:\n    - foo\nstatus: active\n---\nNo heading.\n"
        _write_lesson(tmp_path / "my-lesson.md", content)
        lessons = scan_lessons([tmp_path])
        assert lessons[0]["title"] == "my-lesson"

    def test_skill_md_not_deduped_by_name(self, tmp_path):
        dir_a = tmp_path / "skills" / "alpha"
        dir_b = tmp_path / "skills" / "beta"
        skill_content = "---\nname: alpha-skill\nstatus: active\n---\n# Alpha Skill\nWhen to use: alpha tasks.\n"
        _write_lesson(dir_a / "SKILL.md", skill_content)
        _write_lesson(
            dir_b / "SKILL.md",
            "---\nname: beta-skill\nstatus: active\n---\n# Beta Skill\nWhen: beta tasks.\n",
        )
        lessons = scan_lessons([dir_a, dir_b])
        # SKILL.md files should NOT be deduplicated by filename
        names = {lesson["skill_name"] for lesson in lessons if lesson.get("skill_name")}
        assert "alpha-skill" in names
        assert "beta-skill" in names

    def test_archive_excluded(self, tmp_path):
        _write_lesson(tmp_path / "archive" / "old.md", _basic_lesson(["old"]))
        lessons = scan_lessons([tmp_path])
        assert lessons == []

    def test_archive_in_dir1_shadows_same_name_in_dir2(self, tmp_path):
        # Archiving a lesson locally must also silence the shared contrib copy —
        # otherwise the archive is undone by the next layer. Parity with the
        # brain hook: 8 of 10 leaked lessons in the 2026-08-20 run were exactly this.
        dir1 = tmp_path / "a"
        dir2 = tmp_path / "b"
        _write_lesson(dir1 / "archive" / "foo.md", _basic_lesson(["old-archived"]))
        _write_lesson(dir2 / "foo.md", _basic_lesson(["active-in-b"]))
        assert scan_lessons([dir1, dir2]) == []

    def test_archive_does_not_shadow_active_sibling_in_same_dir(self, tmp_path):
        dir1 = tmp_path / "a"
        dir2 = tmp_path / "b"
        _write_lesson(dir1 / "archive" / "foo.md", _basic_lesson(["old-archived"]))
        _write_lesson(dir1 / "foo.md", _basic_lesson(["active-in-a"]))
        _write_lesson(dir2 / "foo.md", _basic_lesson(["active-in-b"]))
        lessons = scan_lessons([dir1, dir2])
        assert [sorted(lesson["keywords"]) for lesson in lessons] == [["active-in-a"]]

    def test_retired_status_in_dir1_shadows_same_name_in_dir2(self, tmp_path):
        # status: archived/deprecated/automated in an earlier dir suppresses a
        # same-named lesson in a later dir, regardless of category path.
        dir1 = tmp_path / "a"
        dir2 = tmp_path / "b"
        for i, status in enumerate(("archived", "deprecated", "automated")):
            name = f"lesson{i}.md"
            _write_lesson(
                dir1 / "cat-a" / name,
                f"---\nmatch:\n  keywords:\n    - old{i}\nstatus: {status}\n---\n# T\n",
            )
            _write_lesson(dir2 / "cat-b" / name, _basic_lesson([f"contrib{i}"]))
        assert scan_lessons([dir1, dir2]) == []

    def test_no_match_data_in_dir1_shadows_same_name_in_dir2(self, tmp_path):
        dir1 = tmp_path / "a"
        dir2 = tmp_path / "b"
        _write_lesson(
            dir1 / "workflow" / "foo.md",
            "---\ndescription: local rewrite without keywords\nstatus: active\n---\n# T\n",
        )
        _write_lesson(dir2 / "workflow" / "foo.md", _basic_lesson(["contrib"]))
        assert scan_lessons([dir1, dir2]) == []

    def test_retired_skill_md_does_not_shadow_other_skills(self, tmp_path):
        dir1 = tmp_path / "a"
        dir2 = tmp_path / "b"
        _write_lesson(
            dir1 / "old-skill" / "SKILL.md",
            "---\nname: old-skill\nstatus: archived\n---\n# Old\n",
        )
        _write_lesson(
            dir2 / "new-skill" / "SKILL.md",
            "---\nname: new-skill\nstatus: active\n---\n# New\n",
        )
        lessons = scan_lessons([dir1, dir2])
        assert [lesson["skill_name"] for lesson in lessons] == ["new-skill"]

    def test_harness_restrict(self, tmp_path):
        content = (
            "---\nmatch:\n  keywords:\n    - foo\n"
            "metadata:\n  harness:\n    - claude-code\n"
            "status: active\n---\n# Title\n"
        )
        _write_lesson(tmp_path / "foo.md", content)
        lessons = scan_lessons([tmp_path])
        assert lessons[0]["harness_restrict"] == ["claude-code"]


# ---------------------------------------------------------------------------
# filter_by_session_category
# ---------------------------------------------------------------------------


class TestFilterBySessionCategory:
    def _make(self, cats: list[str]) -> dict:
        return {"session_categories": cats, "title": "test"}

    def test_unrestricted_always_passes(self):
        lessons = [self._make([])]
        assert filter_by_session_category(lessons, "code") == lessons
        assert filter_by_session_category(lessons, None) == lessons

    def test_matching_category_passes(self):
        lessons = [self._make(["code", "infrastructure"])]
        assert filter_by_session_category(lessons, "code") == lessons
        assert filter_by_session_category(lessons, "infrastructure") == lessons

    def test_non_matching_category_excluded(self):
        lessons = [self._make(["code"])]
        assert filter_by_session_category(lessons, "social") == []

    def test_unknown_category_excludes_restricted(self):
        lessons = [self._make(["code"])]
        assert filter_by_session_category(lessons, None) == []

    def test_empty_category_is_not_unknown(self):
        matching = self._make([""])
        nonmatching = self._make(["code"])
        assert filter_by_session_category([matching, nonmatching], "") == [matching]

    def test_case_insensitive(self):
        lessons = [self._make(["Code"])]
        assert filter_by_session_category(lessons, "code") == lessons


# ---------------------------------------------------------------------------
# filter_by_harness
# ---------------------------------------------------------------------------


class TestFilterByHarness:
    def _make(self, restrict: list[str]) -> dict:
        return {"harness_restrict": restrict, "title": "test"}

    def test_unrestricted(self):
        lessons = [self._make([])]
        assert filter_by_harness(lessons, "gptme") == lessons

    def test_allowed(self):
        lessons = [self._make(["claude-code"])]
        assert filter_by_harness(lessons, "claude-code") == lessons

    def test_blocked(self):
        lessons = [self._make(["gptme"])]
        assert filter_by_harness(lessons, "claude-code") == []

    def test_case_insensitive(self):
        lessons = [self._make(["Claude-Code"])]
        assert filter_by_harness(lessons, "claude-code") == lessons


# ---------------------------------------------------------------------------
# Holdout filtering
# ---------------------------------------------------------------------------


class TestHoldout:
    def _make(self, path: str, lesson_id: str | None = None) -> dict:
        return {"path": path, "id": lesson_id, "title": "t"}

    def test_parse_holdout_set(self):
        s = parse_holdout_set("foo, bar.md,baz/qux.md")
        assert "foo" in s
        assert "bar.md" in s
        assert "baz/qux.md" in s

    def test_is_held_out_by_stem(self):
        lesson = self._make("/lessons/workflow/foo.md")
        assert is_held_out(lesson, {"foo"})
        assert not is_held_out(lesson, {"bar"})

    def test_is_held_out_by_path_suffix(self):
        lesson = self._make("/lessons/workflow/foo.md")
        assert is_held_out(lesson, {"workflow/foo.md"})

    def test_is_held_out_by_path_suffix_without_extension(self):
        # Regression: the docstring advertises "full or partial path", but a
        # path-shaped token without the .md suffix never matched, silently
        # including a lesson the caller meant to hold out of an A/B run.
        lesson = self._make("/lessons/workflow/foo.md")
        assert is_held_out(lesson, {"workflow/foo"})
        assert not is_held_out(lesson, {"workflow/fo"})
        assert not is_held_out(lesson, {"other/foo"})

    def test_is_held_out_by_relative_path(self):
        lesson = self._make("workflow/foo.md")
        assert is_held_out(lesson, {"workflow/foo"})
        assert is_held_out(lesson, {"workflow/foo.md"})

    def test_is_held_out_by_id(self):
        lesson = self._make("/lessons/foo.md", lesson_id="my-lesson-id")
        assert is_held_out(lesson, {"my-lesson-id"})

    def test_filter_held_out_empty(self):
        lessons = [self._make("/lessons/foo.md")]
        assert filter_held_out(lessons, set()) == lessons

    def test_filter_held_out_removes(self):
        lessons = [
            self._make("/lessons/foo.md"),
            self._make("/lessons/bar.md"),
        ]
        result = filter_held_out(lessons, {"foo"})
        assert len(result) == 1
        assert "bar" in result[0]["path"]


# ---------------------------------------------------------------------------
# BM25 internals
# ---------------------------------------------------------------------------


class TestBM25Internals:
    def test_bm25_min_z_small_corpus(self):
        # n < 3 → -inf (always admit)
        assert _bm25_min_z(0) == -math.inf
        assert _bm25_min_z(1) == -math.inf
        assert _bm25_min_z(2) == -math.inf

    def test_bm25_min_z_scales_with_n(self):
        # For large n approaches BM25_MIN_Z
        large = _bm25_min_z(10000)
        assert abs(large - BM25_MIN_Z) < 0.01

    def test_bm25_min_z_standout_fraction(self):
        # For n=5: max_attainable = 4/sqrt(5) ≈ 1.789 → min_z = 0.8 × 1.789 ≈ 1.43
        result = _bm25_min_z(5)
        expected = BM25_STANDOUT_FRACTION * (4 / math.sqrt(5))
        assert abs(result - expected) < 1e-9

    def test_bm25_zscores_empty(self):
        zs = _bm25_zscores([0.0, 0.0, 0.0])
        assert zs == [0.0, 0.0, 0.0]

    def test_bm25_zscores_one_nonzero(self):
        zs = _bm25_zscores([5.0, 0.0, 0.0])
        assert zs == [0.0, 0.0, 0.0]

    def test_bm25_zscores_two_nonzero(self):
        zs = _bm25_zscores([10.0, 5.0, 0.0])
        # mean=7.5, sd=2.5 → z(10)=1.0, z(5)=-1.0, z(0)=0.0
        assert abs(zs[0] - 1.0) < 1e-9
        assert abs(zs[1] - (-1.0)) < 1e-9
        assert zs[2] == 0.0

    def test_bm25_zscores_uniform(self):
        # All equal → sd=0 → all zeros
        zs = _bm25_zscores([5.0, 5.0, 5.0])
        assert zs == [0.0, 0.0, 0.0]

    def test_bm25_score_zero_for_no_overlap(self):
        index = _build_bm25_index(
            [{"description": "merge conflict", "title": "git", "keywords": [], "when_to_use": ""}]
        )
        score = _bm25_score(["pytest", "testing"], index["corpus"][0], index)
        assert score == 0.0

    def test_bm25_score_positive_for_overlap(self):
        index = _build_bm25_index(
            [
                {
                    "description": "merge conflict",
                    "title": "git",
                    "keywords": ["merge"],
                    "when_to_use": "",
                }
            ]
        )
        score = _bm25_score(["merge", "conflict"], index["corpus"][0], index)
        assert score > 0.0


# ---------------------------------------------------------------------------
# score_lessons (integration)
# ---------------------------------------------------------------------------


class TestScoreLessons:
    def _make_lesson(
        self,
        path: str,
        keywords: list[str],
        description: str = "",
        patterns: list[str] | None = None,
    ) -> dict:
        return {
            "path": path,
            "title": path.split("/")[-1].removesuffix(".md"),
            "id": None,
            "keywords": keywords,
            "patterns": patterns or [],
            "skill_name": None,
            "description": description,
            "when_to_use": "",
            "tags": [],
            "harness_restrict": [],
            "session_categories": [],
            "is_skill": False,
            "body": "",
            "n_keywords": len(keywords),
        }

    def test_keyword_match_scores_higher(self):
        lessons = [
            self._make_lesson("lessons/a.md", ["merge conflict"]),
            self._make_lesson("lessons/b.md", ["testing"]),
        ]
        results = score_lessons(lessons, "I have a merge conflict", use_bm25=False)
        assert len(results) >= 1
        assert results[0]["path"] == "lessons/a.md"

    def test_no_match_returns_empty(self):
        lessons = [self._make_lesson("lessons/a.md", ["merge conflict"])]
        results = score_lessons(lessons, "unrelated query about databases", use_bm25=False)
        assert results == []

    def test_max_results_limit(self):
        lessons = [self._make_lesson(f"lessons/{i}.md", ["merge"]) for i in range(10)]
        results = score_lessons(lessons, "merge conflict", max_results=3, use_bm25=False)
        assert len(results) <= 3

    def test_wildcard_keyword_matches(self):
        lessons = [self._make_lesson("lessons/a.md", ["git*"])]
        results = score_lessons(lessons, "run git status", use_bm25=False)
        assert len(results) == 1

    def test_pattern_match(self):
        lessons = [self._make_lesson("lessons/a.md", [], patterns=[r"already fixed on.*branch"])]
        results = score_lessons(
            lessons, "check if already fixed on the target branch", use_bm25=False
        )
        assert len(results) == 1

    def test_pathological_pattern_times_out(self):
        lessons = [self._make_lesson("lessons/a.md", [], patterns=[r"^(a+)+$"])]
        prompt = "a" * 30_000 + "!"

        assert score_lessons(lessons, prompt, use_bm25=False) == []

    def test_invalid_pattern_is_ignored(self):
        lessons = [self._make_lesson("lessons/a.md", [], patterns=["["])]

        assert score_lessons(lessons, "anything", use_bm25=False) == []

    def test_results_sorted_descending(self):
        lessons = [
            self._make_lesson("lessons/a.md", ["merge"]),
            self._make_lesson("lessons/b.md", ["merge", "conflict"]),
        ]
        results = score_lessons(lessons, "merge conflict", use_bm25=False)
        assert len(results) == 2
        assert results[0]["score"] >= results[1]["score"]

    def test_matched_by_populated(self):
        lessons = [self._make_lesson("lessons/a.md", ["merge conflict"])]
        results = score_lessons(lessons, "merge conflict here", use_bm25=False)
        assert "merge conflict" in results[0]["matched_by"]

    def test_bm25_enabled_finds_semantic_match(self):
        lessons = [
            self._make_lesson(
                "lessons/retrieval.md",
                [],
                description="gptme-rag retrieval upstreaming index semantic search",
            ),
            self._make_lesson("lessons/other.md", [], description="social media posts"),
        ]
        results = score_lessons(lessons, "rag upstreaming semantic retrieval", use_bm25=True)
        # With only one nonzero score, the below-floor corpus bypass admits the
        # sole overlap despite its degenerate z-score.
        assert results, "BM25 should admit the retrieval lesson on a 2-item corpus"
        assert results[0]["path"] == "lessons/retrieval.md"

    def test_bm25_two_nonzero_scores_excludes_weaker_overlap(self, monkeypatch):
        lessons = [
            self._make_lesson("lessons/strong.md", [], description="strong"),
            self._make_lesson("lessons/weak.md", [], description="weak"),
        ]
        monkeypatch.setattr(
            "gptme_rag.lesson_matcher._bm25_score",
            lambda _query, doc, _index: 100.0 if "strong" in doc else 0.1,
        )

        results = score_lessons(lessons, "query", use_bm25=True)

        assert [result["path"] for result in results] == ["lessons/strong.md"]

    def test_bm25_disabled(self):
        lessons = [
            self._make_lesson(
                "lessons/bm25.md",
                [],
                description="merge conflict resolution strategy",
            ),
        ]
        # Without BM25 and no keywords, nothing should match
        results = score_lessons(lessons, "merge conflict", use_bm25=False)
        assert results == []

    def test_score_field_present(self):
        lessons = [self._make_lesson("lessons/a.md", ["merge"])]
        results = score_lessons(lessons, "merge conflict", use_bm25=False)
        assert "score" in results[0]
        assert isinstance(results[0]["score"], float)


# ---------------------------------------------------------------------------
# scan_lessons + score_lessons (filesystem integration)
# ---------------------------------------------------------------------------


class TestScanAndScore:
    def test_end_to_end(self, tmp_path):
        """scan_lessons then score_lessons returns matching lessons."""
        _write_lesson(
            tmp_path / "git-workflow.md",
            (
                "---\nmatch:\n  keywords:\n    - merge conflict\n    - rebase\n"
                "status: active\ndescription: Git workflow patterns for resolving conflicts.\n"
                "---\n# Git Workflow\nBody.\n"
            ),
        )
        _write_lesson(
            tmp_path / "testing.md",
            (
                "---\nmatch:\n  keywords:\n    - pytest\n    - unit test\n"
                "status: active\n---\n# Testing Patterns\nBody.\n"
            ),
        )
        lessons = scan_lessons([tmp_path])
        assert len(lessons) == 2

        results = score_lessons(lessons, "I have a merge conflict to resolve")
        assert any("git-workflow" in r["path"] for r in results)

    def test_session_category_gate_end_to_end(self, tmp_path):
        _write_lesson(
            tmp_path / "code.md",
            (
                "---\nmatch:\n  keywords:\n    - pytest\n"
                "  session_categories:\n    - code\n"
                "status: active\n---\n# Code Patterns\n"
            ),
        )
        _write_lesson(
            tmp_path / "social.md",
            (
                "---\nmatch:\n  keywords:\n    - pytest\n"
                "  session_categories:\n    - social\n"
                "status: active\n---\n# Social Patterns\n"
            ),
        )
        lessons = scan_lessons([tmp_path])
        code_lessons = filter_by_session_category(lessons, "code")
        results = score_lessons(code_lessons, "run pytest", use_bm25=False)
        paths = {r["path"] for r in results}
        assert any("code" in p for p in paths)
        assert not any("social" in p for p in paths)


class TestDescriptorStopwords:
    def test_agent_corpus_generic_tokens_are_stopwords(self):
        from gptme_rag.lesson_matcher import _DESCRIPTOR_STOPWORDS, _descriptor_tokens

        # These fire on nearly every skill in an agent corpus; dropping them from the
        # stopword set made descriptor scores degrade toward a constant (parity run
        # 2026-08-20). Guard the set against being "simplified" again.
        for word in (
            "agent",
            "agents",
            "task",
            "tool",
            "tools",
            "code",
            "project",
            "work",
            "run",
            "build",
            "process",
            "debug",
            "use",
            "using",
        ):
            assert word in _DESCRIPTOR_STOPWORDS, word
        assert len(_DESCRIPTOR_STOPWORDS) >= 53
        assert _descriptor_tokens("agent template onboarding for agents") == {
            "template",
            "onboarding",
        }
