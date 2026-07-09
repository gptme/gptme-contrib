"""Tests for the Claude Code match-lessons hook."""

import builtins
import importlib.util
import sys
from pathlib import Path

import pytest


def load_hook(script_path: Path):
    """Load the match-lessons script as a module."""
    spec = importlib.util.spec_from_file_location("match_lessons", script_path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


HOOK_PATH = (
    Path(__file__).parent.parent / "scripts" / "claude-code-hooks" / "match-lessons.py"
)


@pytest.fixture
def hook():
    return load_hook(HOOK_PATH)


@pytest.fixture
def lesson_dir(tmp_path):
    """Create a temporary lesson directory with a sample lesson."""
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    (lessons / "sample.md").write_text(
        '---\nmatch:\n  keywords:\n    - "merge conflict"\n    - "resolve PR conflicts"\nstatus: active\n---\n# Sample Lesson\n\n## Rule\nDo the thing.\n'
    )
    (lessons / "archived.md").write_text(
        '---\nmatch:\n  keywords:\n    - "merge conflict"\nstatus: archived\n---\n# Archived Lesson\n\nOld content.\n'
    )
    return lessons


@pytest.fixture
def workspace(tmp_path, lesson_dir):
    """Create a minimal gptme workspace."""
    (tmp_path / "gptme.toml").write_text('[lessons]\ndirs = ["lessons"]\n')
    return tmp_path


# --- keyword_to_regex ---


def test_keyword_to_regex_basic(hook):
    pattern = hook.keyword_to_regex("merge conflict")
    assert pattern is not None
    assert pattern.search("there is a merge conflict here")


def test_keyword_to_regex_case_insensitive(hook):
    pattern = hook.keyword_to_regex("Merge Conflict")
    assert pattern is not None
    assert pattern.search("MERGE CONFLICT")


def test_keyword_to_regex_wildcard(hook):
    pattern = hook.keyword_to_regex("git * failed")
    assert pattern is not None
    assert pattern.search("git push failed")
    assert pattern.search("git rebase failed")


def test_keyword_to_regex_bare_star_returns_none(hook):
    assert hook.keyword_to_regex("*") is None


def test_keyword_to_regex_empty_returns_none(hook):
    assert hook.keyword_to_regex("") is None


# --- scan_lessons ---


def test_scan_lessons_basic(hook, lesson_dir):
    lessons = hook.scan_lessons([lesson_dir])
    assert len(lessons) == 1  # archived lesson excluded
    assert lessons[0]["title"] == "Sample Lesson"
    assert "merge conflict" in lessons[0]["keywords"]


def test_scan_lessons_skips_readme(hook, tmp_path):
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "README.md").write_text("# README\nNot a lesson.\n")
    (lessons_dir / "real.md").write_text(
        '---\nmatch:\n  keywords:\n    - "real keyword"\nstatus: active\n---\n# Real\n\nContent.\n'
    )
    result = hook.scan_lessons([lessons_dir])
    assert len(result) == 1
    assert result[0]["title"] == "Real"


def test_scan_lessons_skips_node_modules(hook, tmp_path):
    """Files under node_modules must not be included, even with valid frontmatter."""
    lessons_dir = tmp_path / "skills"
    lessons_dir.mkdir()
    # A legitimate skill
    (lessons_dir / "real-skill.md").write_text(
        "---\nname: real-skill\ndescription: A real skill\nstatus: active\n---\n# Real Skill\n\nContent.\n"
    )
    # An npm package agent file — should be excluded
    node_agents = (
        lessons_dir / "my-tool" / "node_modules" / "playwright" / "lib" / "agents"
    )
    node_agents.mkdir(parents=True)
    (node_agents / "playwright-test-generator.agent.md").write_text(
        "---\nname: playwright-test-generator\ndescription: Use this agent to generate Playwright tests\n---\n# Generator\n\nContent.\n"
    )
    result = hook.scan_lessons([lessons_dir])
    paths = [r["path"] for r in result]
    assert all(
        "node_modules" not in p for p in paths
    ), f"node_modules file leaked into scan: {paths}"
    assert len(result) == 1
    assert result[0]["title"] == "Real Skill"


def test_scan_lessons_skips_git_and_cache(hook, tmp_path):
    """Files under .git / __pycache__ / .venv must not be included."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "good.md").write_text(
        '---\nmatch:\n  keywords:\n    - "good"\nstatus: active\n---\n# Good\n\nContent.\n'
    )
    for skip_dir in (".git", "__pycache__", ".venv"):
        d = lessons_dir / skip_dir
        d.mkdir()
        (d / "hidden.md").write_text(
            '---\nmatch:\n  keywords:\n    - "hidden"\nstatus: active\n---\n# Hidden\n\nContent.\n'
        )
    result = hook.scan_lessons([lessons_dir])
    assert len(result) == 1
    assert result[0]["title"] == "Good"


def test_scan_lessons_dedup_by_filename(hook, tmp_path):
    """First-dir-wins deduplication by filename."""
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()
    (dir1 / "foo.md").write_text(
        '---\nmatch:\n  keywords:\n    - "dir1 lesson"\nstatus: active\n---\n# Dir1 Lesson\n\nContent from dir1.\n'
    )
    (dir2 / "foo.md").write_text(
        '---\nmatch:\n  keywords:\n    - "dir2 lesson"\nstatus: active\n---\n# Dir2 Lesson\n\nContent from dir2.\n'
    )
    result = hook.scan_lessons([dir1, dir2])
    assert len(result) == 1
    assert "dir1 lesson" in result[0]["keywords"]


# --- score_lessons ---


def test_score_lessons_matches(hook, lesson_dir):
    lessons = hook.scan_lessons([lesson_dir])
    results = hook.score_lessons(lessons, "I have a merge conflict to resolve")
    assert len(results) == 1
    assert results[0]["title"] == "Sample Lesson"


def test_score_lessons_no_match(hook, lesson_dir):
    lessons = hook.scan_lessons([lesson_dir])
    results = hook.score_lessons(lessons, "something completely unrelated")
    assert results == []


def test_score_lessons_respects_max(hook, tmp_path):
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    for i in range(5):
        (lessons_dir / f"lesson{i}.md").write_text(
            f'---\nmatch:\n  keywords:\n    - "common keyword {i}"\n    - "trigger word"\nstatus: active\n---\n# Lesson {i}\n\nContent {i}.\n'
        )
    lessons = hook.scan_lessons([lessons_dir])
    results = hook.score_lessons(lessons, "trigger word is here", max_results=3)
    assert len(results) <= 3


# --- session state ---


def test_session_state_roundtrip(hook, tmp_path, monkeypatch):
    """Session state saves and loads correctly."""
    monkeypatch.setattr(hook, "STATE_DIR", tmp_path / "state")
    state = {"injected": ["/path/to/lesson.md"], "last_pretool": 1234567890.0}
    hook.save_session_state("test-session", state)
    loaded = hook.load_session_state("test-session")
    assert loaded["injected"] == ["/path/to/lesson.md"]
    assert loaded["last_pretool"] == 1234567890.0


def test_session_state_empty_for_new_session(hook, tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "STATE_DIR", tmp_path / "state")
    state = hook.load_session_state("brand-new-session")
    assert state == {"injected": [], "last_pretool": 0}


# --- find_workspace ---


def test_find_workspace_uses_cwd(hook, workspace, monkeypatch):
    """Workspace is found from cwd when script is not inside workspace."""
    monkeypatch.chdir(workspace)
    # Reset cached workspace
    hook._workspace = None
    found = hook.find_workspace()
    assert found == workspace
    hook._workspace = None  # Reset after test


def test_find_workspace_walks_up(hook, workspace, tmp_path, monkeypatch):
    """Workspace found by walking up from subdirectory."""
    subdir = workspace / "subdir" / "deep"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    hook._workspace = None
    found = hook.find_workspace()
    assert found == workspace
    hook._workspace = None


# --- format_lessons ---


def test_format_lessons_basic(hook, lesson_dir):
    lessons = hook.scan_lessons([lesson_dir])
    matched = hook.score_lessons(lessons, "merge conflict")
    context = hook.format_lessons(matched, set())
    assert "## Matched Lessons" in context
    assert "Sample Lesson" in context
    assert "Do the thing" in context


def test_format_lessons_skips_already_injected(hook, lesson_dir):
    lessons = hook.scan_lessons([lesson_dir])
    matched = hook.score_lessons(lessons, "merge conflict")
    already = {matched[0]["path"]}
    context = hook.format_lessons(matched, already)
    assert context == ""


def test_format_lessons_includes_source(hook, lesson_dir):
    lessons = hook.scan_lessons([lesson_dir])
    matched = hook.score_lessons(lessons, "merge conflict")
    context = hook.format_lessons(matched, set())
    assert "*Source:" in context


# --- extract_frontmatter ---


def test_extract_frontmatter_yaml(hook):
    content = '---\nmatch:\n  keywords:\n    - "test keyword"\nstatus: active\n---\n\n# Title\n\nBody text.\n'
    fm, body = hook.extract_frontmatter(content)
    assert fm.get("status") == "active"
    assert "test keyword" in fm["match"]["keywords"]
    assert "Title" in body


def test_extract_frontmatter_no_frontmatter(hook):
    content = "# Just a title\n\nBody."
    fm, body = hook.extract_frontmatter(content)
    assert fm == {}
    assert "Just a title" in body


def test_extract_frontmatter_archived_excluded(hook, lesson_dir):
    """scan_lessons excludes archived lessons."""
    lessons = hook.scan_lessons([lesson_dir])
    titles = [lesson["title"] for lesson in lessons]
    assert "Archived Lesson" not in titles


def test_extract_frontmatter_regex_fallback_keeps_multiline_scalars(hook, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError
        return real_import(name, *args, **kwargs)

    # Remove yaml from the import cache so builtins.__import__ is actually called.
    # Without this, `import yaml` inside extract_frontmatter resolves from sys.modules
    # without invoking __import__, silently exercising the yaml path instead.
    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    content = (
        "---\n"
        "description: |\n"
        "  First line.\n"
        "  Second line.\n"
        "when_to_use: >\n"
        "  Use this skill\n"
        "  when routing work.\n"
        "---\n\n"
        "# Title\n"
    )

    fm, _ = hook.extract_frontmatter(content)

    assert fm["description"] == "First line.\nSecond line."
    assert fm["when_to_use"] == "Use this skill when routing work."


def test_scan_lessons_dedupes_top_level_keywords_and_patterns(hook, tmp_path):
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "dedupe.md").write_text(
        "---\n"
        "match:\n"
        '  keywords:\n    - "duplicate keyword"\n'
        '  patterns:\n    - "duplicate.*pattern"\n'
        'keywords: ["duplicate keyword", "extra keyword"]\n'
        'patterns: ["duplicate.*pattern"]\n'
        "status: active\n"
        "---\n"
        "# Dedupe\n"
    )

    lessons = hook.scan_lessons([lessons_dir])
    assert lessons[0]["keywords"] == ["duplicate keyword", "extra keyword"]
    assert lessons[0]["patterns"] == ["duplicate.*pattern"]

    matches = hook.score_lessons(lessons, "duplicate keyword and duplicateXpattern")
    assert matches[0]["matched_by"].count("duplicate keyword") == 1
    assert matches[0]["matched_by"].count("pattern:duplicate.*pattern") == 1


def test_detect_harness_defaults_to_gptme(hook, monkeypatch):
    # No env vars set → fall back to gptme (the primary harness)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CODEX", raising=False)
    monkeypatch.delenv("CODEX_INSTALLED", raising=False)
    assert hook.detect_harness() == "gptme"


# --- build_pretool_match_text ---


def test_build_pretool_match_text_file_path(hook):
    """file_path field is extracted for Read/Write tools."""
    text = hook.build_pretool_match_text("Read", {"file_path": "/path/to/file.py"})
    assert "/path/to/file.py" in text


def test_build_pretool_match_text_command(hook):
    """command field is extracted for Bash tool."""
    text = hook.build_pretool_match_text(
        "Bash", {"command": "git rebase origin/master"}
    )
    assert "git rebase origin/master" in text


def test_build_pretool_match_text_multiple_fields(hook):
    """Multiple known fields are all included in output."""
    text = hook.build_pretool_match_text(
        "Grep",
        {"pattern": "merge conflict", "file_path": "src/foo.py"},
    )
    assert "merge conflict" in text
    assert "src/foo.py" in text


def test_build_pretool_match_text_unknown_fields_ignored(hook):
    """Fields not in the known list are ignored."""
    text = hook.build_pretool_match_text("Bash", {"unknown_key": "should not appear"})
    assert "should not appear" not in text


def test_build_pretool_match_text_empty_input(hook):
    """Empty tool input returns empty string."""
    text = hook.build_pretool_match_text("Bash", {})
    assert text == ""


# --- holdout filtering ---


def test_parse_holdout_empty(hook, monkeypatch):
    """Empty env produces empty set."""
    monkeypatch.delenv("HOLDOUT_LESSONS", raising=False)
    assert hook.parse_holdout_lessons_env("") == set()
    assert hook.parse_holdout_lessons_env(None) == set()


def test_parse_holdout_single(hook):
    result = hook.parse_holdout_lessons_env("browser-verification")
    assert result == {"browser-verification"}


def test_parse_holdout_multiple(hook):
    result = hook.parse_holdout_lessons_env("foo,bar, baz ")
    assert result == {"foo", "bar", "baz"}


def test_parse_holdout_normalizes_backslash(hook):
    result = hook.parse_holdout_lessons_env("lessons\\tools\\foo.md")
    assert "lessons/tools/foo.md" in result


def test_is_held_out_by_stem(hook):
    lesson = {"path": "/workspace/lessons/tools/browser-verification.md"}
    assert hook.is_held_out_lesson(lesson, {"browser-verification"})


def test_is_held_out_by_filename(hook):
    lesson = {"path": "/workspace/lessons/tools/browser-verification.md"}
    assert hook.is_held_out_lesson(lesson, {"browser-verification.md"})


def test_is_held_out_by_partial_path(hook):
    lesson = {"path": "/workspace/lessons/tools/browser-verification.md"}
    assert hook.is_held_out_lesson(lesson, {"tools/browser-verification.md"})


def test_is_held_out_by_id(hook):
    lesson = {"path": "/workspace/lessons/foo.md", "id": "my-custom-id"}
    assert hook.is_held_out_lesson(lesson, {"my-custom-id"})


def test_is_held_out_skill_uses_parent_dir(hook):
    lesson = {"path": "/workspace/skills/my-skill/SKILL.md"}
    assert hook.is_held_out_lesson(lesson, {"my-skill"})


def test_not_held_out_when_no_match(hook):
    lesson = {"path": "/workspace/lessons/tools/browser-verification.md"}
    assert not hook.is_held_out_lesson(lesson, {"unrelated-lesson"})


def test_not_held_out_empty_set(hook):
    lesson = {"path": "/workspace/lessons/tools/browser-verification.md"}
    assert not hook.is_held_out_lesson(lesson, set())


def test_filter_held_out_lessons(hook):
    lessons = [
        {"path": "/workspace/lessons/tools/browser-verification.md", "title": "BV"},
        {"path": "/workspace/lessons/tools/git-workflow.md", "title": "GW"},
        {"path": "/workspace/lessons/social/twitter.md", "title": "TW"},
    ]
    holdout = {"browser-verification", "twitter.md"}
    result = hook.filter_held_out_lessons(lessons, holdout)
    assert len(result) == 1
    assert result[0]["title"] == "GW"


def test_filter_held_out_empty_passthrough(hook):
    lessons = [{"path": "/a.md", "title": "A"}]
    assert hook.filter_held_out_lessons(lessons, set()) == lessons


def test_scan_lessons_includes_id(hook, tmp_path):
    """scan_lessons extracts id field from frontmatter."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "with_id.md").write_text(
        '---\nid: custom-lesson-id\nmatch:\n  keywords:\n    - "test"\nstatus: active\n---\n# With ID\n\nContent.\n'
    )
    (lessons_dir / "no_id.md").write_text(
        '---\nmatch:\n  keywords:\n    - "other"\nstatus: active\n---\n# No ID\n\nContent.\n'
    )
    results = hook.scan_lessons([lessons_dir])
    by_title = {r["title"]: r for r in results}
    assert by_title["With ID"]["id"] == "custom-lesson-id"
    assert by_title["No ID"]["id"] is None


# --- session_categories ---


def test_scan_lessons_extracts_session_categories(hook, tmp_path):
    """scan_lessons populates session_categories from match.session_categories."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "categorized.md").write_text(
        "---\nmatch:\n  keywords:\n    - trigger\n  session_categories:\n    - code\n    - infrastructure\nstatus: active\n---\n# Categorized\n\nContent.\n"
    )
    (lessons_dir / "uncategorized.md").write_text(
        "---\nmatch:\n  keywords:\n    - other\nstatus: active\n---\n# Uncategorized\n\nContent.\n"
    )
    results = hook.scan_lessons([lessons_dir])
    by_title = {r["title"]: r for r in results}
    assert by_title["Categorized"]["session_categories"] == ["code", "infrastructure"]
    assert by_title["Uncategorized"]["session_categories"] == []


def test_filter_by_session_category_keeps_unrestricted(hook, tmp_path):
    """Lessons without session_categories pass through regardless of category."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "unrestricted.md").write_text(
        "---\nmatch:\n  keywords:\n    - keyword\nstatus: active\n---\n# Unrestricted\n\nContent.\n"
    )
    lessons = hook.scan_lessons([lessons_dir])
    result = hook.filter_by_session_category(lessons, "cleanup")
    assert len(result) == 1


def test_filter_by_session_category_keeps_matching(hook, tmp_path):
    """Lessons whose session_categories include current category are kept."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "code-only.md").write_text(
        "---\nmatch:\n  keywords:\n    - code\n  session_categories:\n    - code\nstatus: active\n---\n# Code Only\n\nContent.\n"
    )
    lessons = hook.scan_lessons([lessons_dir])
    kept = hook.filter_by_session_category(lessons, "code")
    assert len(kept) == 1
    dropped = hook.filter_by_session_category(lessons, "cleanup")
    assert len(dropped) == 0


def test_filter_by_session_category_none_keeps_all(hook, tmp_path):
    """Unknown category (None) keeps all lessons unchanged."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "restricted.md").write_text(
        "---\nmatch:\n  keywords:\n    - x\n  session_categories:\n    - strategic\nstatus: active\n---\n# Restricted\n\nContent.\n"
    )
    lessons = hook.scan_lessons([lessons_dir])
    result = hook.filter_by_session_category(lessons, None)
    assert len(result) == 1


def test_detect_session_category_from_env(hook, monkeypatch):
    """detect_session_category reads CASCADE_CATEGORY env var."""
    monkeypatch.setenv("CASCADE_CATEGORY", "research")
    for var in ("CASCADE_EXECUTION_CATEGORY", "GRADE_CATEGORY", "WORKER_CATEGORY"):
        monkeypatch.delenv(var, raising=False)
    assert hook.detect_session_category() == "research"


def test_detect_session_category_fallback_vars(hook, monkeypatch):
    """detect_session_category falls back to GRADE_CATEGORY when CASCADE_CATEGORY unset."""
    monkeypatch.delenv("CASCADE_CATEGORY", raising=False)
    monkeypatch.delenv("CASCADE_EXECUTION_CATEGORY", raising=False)
    monkeypatch.setenv("GRADE_CATEGORY", "social")
    monkeypatch.delenv("WORKER_CATEGORY", raising=False)
    assert hook.detect_session_category() == "social"


def test_detect_session_category_none_when_unset(hook, monkeypatch):
    """detect_session_category returns None when no category env vars are set."""
    for var in (
        "CASCADE_CATEGORY",
        "CASCADE_EXECUTION_CATEGORY",
        "GRADE_CATEGORY",
        "WORKER_CATEGORY",
    ):
        monkeypatch.delenv(var, raising=False)
    assert hook.detect_session_category() is None


# --- BM25 scoring ---


def test_bm25_index_built_from_lessons(hook, tmp_path):
    """_build_bm25_index returns a valid index with corpus matching lesson count."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    for i in range(3):
        (lessons_dir / f"lesson{i}.md").write_text(
            f"---\ndescription: lesson about topic {i}\nmatch:\n  keywords:\n    - kw{i}\nstatus: active\n---\n# Lesson {i}\n\nContent.\n"
        )
    lessons = hook.scan_lessons([lessons_dir])
    index = hook._build_bm25_index(lessons)
    assert len(index["corpus"]) == 3
    assert index["N"] == 3
    assert index["avg_dl"] > 0


def test_bm25_score_relevant_higher_than_unrelated(hook):
    """_bm25_score gives higher scores to relevant docs than unrelated ones."""
    doc_relevant = ["python", "merge", "conflict", "resolution"]
    doc_unrelated = ["javascript", "async", "promise", "then"]
    index = {
        "corpus": [doc_relevant, doc_unrelated],
        "df": {
            "python": 1,
            "merge": 1,
            "conflict": 1,
            "resolution": 1,
            "javascript": 1,
            "async": 1,
            "promise": 1,
            "then": 1,
        },
        "N": 2,
        "avg_dl": 4.0,
    }
    query = ["merge", "conflict"]
    score_relevant = hook._bm25_score(query, doc_relevant, index)
    score_unrelated = hook._bm25_score(query, doc_unrelated, index)
    assert score_relevant > score_unrelated
    assert score_unrelated == 0.0


def test_score_lessons_with_bm25_index(hook, tmp_path):
    """score_lessons accepts bm25_index and does not crash."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "lesson.md").write_text(
        "---\ndescription: handles merge conflict resolution\nmatch:\n  keywords:\n    - merge conflict\nstatus: active\n---\n# Merge Lesson\n\nContent.\n"
    )
    lessons = hook.scan_lessons([lessons_dir])
    index = hook._build_bm25_index(lessons)
    results = hook.score_lessons(lessons, "merge conflict resolution", bm25_index=index)
    assert len(results) == 1
    # BM25 match tag should appear in matched_by
    has_bm25 = any("bm25" in tag for tag in results[0].get("matched_by", []))
    assert has_bm25
