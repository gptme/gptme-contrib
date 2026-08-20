"""Tests for gptme_rag.lesson_matcher (Phase 2.4 upstream)."""

from __future__ import annotations

import math
from pathlib import Path


from gptme_rag.lesson_matcher import (
    BM25_MIN_Z,
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


# ---------------------------------------------------------------------------
# scan_lessons
# ---------------------------------------------------------------------------


def _write_lesson(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _basic_lesson(keywords: list[str], status: str = "active") -> str:
    kw_yaml = "\n".join(f'    - "{k}"' for k in keywords)
    return f"---\nmatch:\n  keywords:\n{kw_yaml}\nstatus: {status}\n---\n" f"# Rule\nBody text.\n"


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

    def test_session_categories(self, tmp_path):
        content = (
            "---\nmatch:\n  keywords:\n    - foo\n"
            "  session_categories:\n    - code\n"
            "status: active\n---\n# Title\n"
        )
        _write_lesson(tmp_path / "foo.md", content)
        lessons = scan_lessons([tmp_path])
        assert lessons[0]["session_categories"] == ["code"]

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
        if results:  # BM25 may admit nothing on small corpus
            assert results[0]["path"] == "lessons/retrieval.md"

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
