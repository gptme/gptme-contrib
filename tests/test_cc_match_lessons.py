"""Tests for the Claude Code match-lessons hook."""

import builtins
import importlib.util
import json
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


def test_skill_cap_preserves_lessons_and_exempt_skills(hook, monkeypatch):
    monkeypatch.setattr(
        hook,
        "_classify_lesson",
        lambda path: ("exempt" if path == "guard" else "unknown", 1),
    )
    matches = [
        {"path": "lesson", "is_skill": False},
        {"path": "skill-a", "is_skill": True},
        {"path": "guard", "is_skill": True},
        {"path": "skill-b", "is_skill": True},
    ]
    predicted = [{"path": "skill-c", "is_skill": True}]

    kept_matches, kept_predicted = hook.apply_skill_cap(matches, predicted, cap=1)

    assert [item["path"] for item in kept_matches] == ["lesson", "skill-a", "guard"]
    assert kept_predicted == []


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


def test_main_applies_skill_cap_after_dropout(hook, tmp_path, monkeypatch):
    """A skill withheld by dropout must not consume the treatment cap budget.

    Pre-fix ordering capped first (keeping ``skill-a``), then dropout withheld
    it, leaving zero skills injected even though ``skill-b``/``skill-c`` could
    still render. Post-fix, dropout runs first and the cap slot is refilled.
    """
    import io

    monkeypatch.setattr(hook, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(hook, "get_workspace", lambda: tmp_path)

    lessons = [
        {"path": "skill-a", "title": "A", "is_skill": True, "score": 1.0},
        {"path": "skill-b", "title": "B", "is_skill": True, "score": 1.0},
    ]
    monkeypatch.setattr(hook, "load_lesson_dirs", lambda ws: [tmp_path])
    monkeypatch.setattr(hook, "scan_lessons", lambda dirs: lessons)
    monkeypatch.setattr(hook, "detect_harness", lambda: "claude-code")
    monkeypatch.setattr(hook, "detect_session_category", lambda: None)
    monkeypatch.setattr(hook, "filter_by_harness", lambda items, harness: items)
    monkeypatch.setattr(hook, "filter_by_session_category", lambda items, cat: items)
    monkeypatch.setattr(hook, "filter_by_repo", lambda items, repo: items)
    monkeypatch.setattr(hook, "get_already_injected", lambda sid, tp=None: set())
    monkeypatch.setattr(hook, "score_lessons", lambda *a, **k: list(lessons))
    # Keep the test hermetic: real score_memories reads the workspace and may
    # inject memory hits when gptme happens to be importable.
    monkeypatch.setattr(hook, "score_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        hook,
        "get_predicted_lessons",
        lambda *a, **k: [
            {"path": "skill-c", "title": "C", "is_skill": True, "score": 1.0}
        ],
    )
    monkeypatch.setattr(hook, "filter_held_out_lessons", lambda items, holdout: items)

    def fake_dropout(matches, predicted, sid, ws, **kwargs):
        # Simulate class-aware dropout withholding skill-a this invocation.
        return [m for m in matches if m["path"] != "skill-a"], predicted

    monkeypatch.setattr(hook, "_apply_lesson_dropout_multi", fake_dropout)
    monkeypatch.setattr(hook, "_log_lesson_events", lambda *a, **k: None)
    monkeypatch.setattr(hook, "extract_tool_sequence", lambda tp: [])
    monkeypatch.setattr(hook, "log_trajectory_match", lambda *a, **k: None)

    captured: dict[str, list[str]] = {}

    def fake_format(matches, already, predicted, event_type):
        captured["matches"] = [m["path"] for m in matches]
        captured["predicted"] = [p["path"] for p in predicted]
        return "ctx"

    monkeypatch.setattr(hook, "format_lessons", fake_format)

    hook_input = {
        "hook_event_name": "PreToolUse",
        "session_id": "order-test",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook_input)))

    with pytest.raises(SystemExit):
        hook.main()

    assert captured["matches"] == ["skill-b"], captured
    assert captured["predicted"] == [], captured


# --- find_workspace ---


def test_find_workspace_uses_cwd(hook, workspace, tmp_path_factory, monkeypatch):
    """Workspace is found from cwd when script is not inside workspace."""
    # Point __file__ into a SEPARATE tmp directory (not a subdirectory of the
    # workspace fixture's tmp_path) so the script-dir walk finds no gptme.toml
    # and falls through to cwd.  Using tmp_path_factory.mktemp() gives an
    # isolated root that is guaranteed to be outside the workspace tree.
    hook_dir = tmp_path_factory.mktemp("isolated_hook")
    monkeypatch.setattr(hook, "__file__", str(hook_dir / "match-lessons.py"))
    monkeypatch.chdir(workspace)
    # Reset cached workspace
    hook._workspace = None
    found = hook.find_workspace()
    assert found == workspace
    hook._workspace = None  # Reset after test


def test_find_workspace_walks_up(hook, workspace, tmp_path_factory, monkeypatch):
    """Workspace found by walking up from subdirectory."""
    # Point __file__ into a SEPARATE tmp directory (not a subdirectory of the
    # workspace fixture's tmp_path) so the script-dir walk finds no gptme.toml.
    hook_dir = tmp_path_factory.mktemp("isolated_hook")
    monkeypatch.setattr(hook, "__file__", str(hook_dir / "match-lessons.py"))
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


# --- _strip_quoted_spans ---


def test_strip_quoted_spans_single_quotes(hook):
    """Single-quoted spans are removed."""
    result = hook._strip_quoted_spans("grep 'SKILL\\|skill' file.py")
    assert "SKILL" not in result
    assert "skill" not in result
    assert "grep" in result
    assert "file.py" in result


def test_strip_quoted_spans_double_quotes(hook):
    """Double-quoted spans are removed."""
    result = hook._strip_quoted_spans('python -c "import foo; print(foo)"')
    assert "import foo" not in result
    assert "python" in result


def test_strip_quoted_spans_preserves_unquoted(hook):
    """Unquoted tokens like subcommands and paths are preserved."""
    result = hook._strip_quoted_spans("git commit -m 'fix stuff' file.py")
    assert "git" in result
    assert "commit" in result
    assert "file.py" in result
    assert "fix stuff" not in result


def test_strip_quoted_spans_empty(hook):
    """Empty string returns empty string."""
    assert hook._strip_quoted_spans("") == ""


def test_strip_quoted_spans_no_quotes(hook):
    """Text without quotes is returned unchanged (modulo whitespace)."""
    result = hook._strip_quoted_spans("git rebase origin/master")
    assert "git" in result
    assert "rebase" in result
    assert "origin/master" in result


def test_strip_quoted_spans_heredoc_body(hook):
    """Heredoc bodies are removed up to the delimiter; text after it survives."""
    cmd = "cat > notes.md << 'EOF'\nsubscription skill factory\nEOF\ngit status"
    result = hook._strip_quoted_spans(cmd)
    assert "subscription" not in result
    assert "factory" not in result
    assert "notes.md" in result
    assert "git status" in result


def test_strip_quoted_spans_heredoc_body_starting_with_word(hook):
    """A body line starting with a word char does not end the heredoc early."""
    cmd = "python3 - <<PY\nimport skill\nprint(skill)\nPY\nls"
    result = hook._strip_quoted_spans(cmd)
    assert "skill" not in result
    assert "ls" in result


def test_strip_quoted_spans_heredoc_in_commit_message(hook):
    """The git commit -m "$(cat <<'EOF' ... EOF)" idiom strips the message."""
    cmd = "git commit -m \"$(cat <<'EOF'\nfeat: subscription skill\nEOF\n)\" file.py"
    result = hook._strip_quoted_spans(cmd)
    assert "subscription" not in result
    assert "git commit" in result
    assert "file.py" in result


def test_strip_quoted_spans_herestring_not_heredoc(hook):
    """A here-string (<<<) is not a heredoc and must not swallow later lines."""
    cmd = "grep x <<<foo\ngit rebase origin/master"
    result = hook._strip_quoted_spans(cmd)
    assert "git rebase" in result


# --- build_pretool_match_text: grep-pattern misfire class (session-72ce fixtures) ---


def test_grep_pattern_single_quoted_does_not_match_skill(hook, tmp_path):
    """grep 'SKILL\\|skill' must NOT trigger lesson injection via the literal.

    Session-72ce misfire: grep 'SKILL\\|skill' injected Subscription Management
    skill because the quoted literal 'SKILL\\|skill' matched the 'SKILL' keyword.
    After stripping, only 'grep' remains in the command match text — not enough
    to match skill keywords.
    """
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "subscription-management.md").write_text(
        '---\nmatch:\n  keywords:\n    - "SKILL"\n    - "skill"\nstatus: active\n---\n# Subscription Management\n\n## Rule\nManage subscriptions.\n'
    )
    lessons = hook.scan_lessons([lessons_dir])

    # Build match text the same way the hook does for PreToolUse
    text = hook.build_pretool_match_text("Bash", {"command": "grep 'SKILL\\|skill' ."})
    results = hook.score_lessons(lessons, text)

    assert (
        results == []
    ), f"grep 'SKILL\\|skill' should not trigger subscription lesson; got: {[r['title'] for r in results]}"


def test_grep_context_tier_does_not_match_factory(hook, tmp_path):
    """grep 'context_tier' must NOT trigger Factory-Asset 3D skill injection.

    Session-72ce misfire: grep 'context_tier' matched keywords in the factory
    skill. After stripping, 'context_tier' (the data) is removed.
    """
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "factory-asset-3d.md").write_text(
        '---\nmatch:\n  keywords:\n    - "context_tier"\n    - "factory asset"\nstatus: active\n---\n# Factory Asset 3D\n\n## Rule\nBuild factory assets.\n'
    )
    lessons = hook.scan_lessons([lessons_dir])

    text = hook.build_pretool_match_text("Bash", {"command": "grep 'context_tier' ."})
    results = hook.score_lessons(lessons, text)

    assert (
        results == []
    ), f"grep 'context_tier' should not trigger factory lesson; got: {[r['title'] for r in results]}"


def test_git_commit_still_matches_commit_lesson(hook, tmp_path):
    """Genuine git commit command still matches the commit-workflow lesson.

    Regression guard: unquoted tokens like 'commit' and 'conventional' must
    survive the stripping step so genuinely relevant lessons still fire.
    """
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "conventional-commits.md").write_text(
        '---\nmatch:\n  keywords:\n    - "conventional commit"\n    - "git commit"\nstatus: active\n---\n# Conventional Commits\n\n## Rule\nUse conventional commits.\n'
    )
    lessons = hook.scan_lessons([lessons_dir])

    text = hook.build_pretool_match_text(
        "Bash", {"command": "git commit -m 'fix: update' file.py"}
    )
    results = hook.score_lessons(lessons, text)

    # 'git commit' (unquoted) must still match; 'fix: update' (quoted) is stripped
    assert any(
        "Conventional Commits" == r["title"] for r in results
    ), f"git commit command should still match commit lesson; got: {[r['title'] for r in results]}"


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


def test_filter_by_session_category_case_insensitive(hook, tmp_path):
    """YAML session_categories are matched case-insensitively against the detected (lowercase) category."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "code-only.md").write_text(
        "---\nmatch:\n  keywords:\n    - code\n  session_categories:\n    - Code\nstatus: active\n---\n# Code Only Mixed Case\n\nContent.\n"
    )
    lessons = hook.scan_lessons([lessons_dir])
    # detect_session_category always returns lowercase; YAML "Code" must still match
    kept = hook.filter_by_session_category(lessons, "code")
    assert (
        len(kept) == 1
    ), "Mixed-case YAML category should match lowercase env-var category"
    dropped = hook.filter_by_session_category(lessons, "cleanup")
    assert len(dropped) == 0


def test_filter_by_session_category_none_filters_restricted(hook, tmp_path):
    """Unknown category (None) filters out lessons with session_categories restrictions."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "restricted.md").write_text(
        "---\nmatch:\n  keywords:\n    - x\n  session_categories:\n    - strategic\nstatus: active\n---\n# Restricted\n\nContent.\n"
    )
    (lessons_dir / "universal.md").write_text(
        "---\nmatch:\n  keywords:\n    - y\nstatus: active\n---\n# Universal\n\nContent.\n"
    )
    lessons = hook.scan_lessons([lessons_dir])
    result = hook.filter_by_session_category(lessons, None)
    # Restricted lesson must be filtered; universal lesson must pass through
    titles = {lesson["title"] for lesson in result}
    assert "Restricted" not in titles
    assert "Universal" in titles


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
# Note: tests for _build_bm25_index, _bm25_score, _bm25_min_z, _bm25_zscores
# belong in gptme-rag tests — those functions moved to gptme_rag.lesson_matcher.


def test_bm25_min_raw_floor_calibration(hook):
    """_BM25_MIN_RAW must stay at the recalibrated production floor (>= 40.0).

    Pins the threshold so future changes don't silently restore the 0.8 value
    that never filtered anything (measured 2026-08-05: real scores 3.92–1397.22,
    median 60.80).
    """
    assert hook._BM25_MIN_RAW >= 40.0, (
        f"_BM25_MIN_RAW ({hook._BM25_MIN_RAW}) regressed below the recalibrated "
        f"floor of 40.0 — restore or re-calibrate"
    )


# --- Policy manifest / classify_lesson ---


def test_classify_lesson_absolute_path(hook, tmp_path):
    """P1 fix: _classify_lesson must handle absolute lesson paths from scan_lessons.

    scan_lessons stores paths as str(f) where f is an absolute Path, so
    lesson_path looks like /home/bob/bob/lessons/patterns/foo.md.
    Before the fix, strip("/").replace("lessons/", "", 1) produced
    home/bob/bob/patterns/foo which never matched any manifest key.
    """
    manifest = {
        "version": 1,
        "updated_at": "",
        "validated_core": [],
        "exempt": [],
        "holdout_population": ["patterns/persistent-learning"],
    }
    hook._policy_manifest_cache = manifest

    abs_path = "/home/bob/bob/lessons/patterns/persistent-learning.md"
    policy_class, version = hook._classify_lesson(abs_path)

    hook._policy_manifest_cache = None  # reset cache

    assert policy_class == "holdout", (
        f"Expected 'holdout' for absolute path {abs_path!r}, got {policy_class!r}. "
        "Path normalization must extract the part after 'lessons/' regardless of prefix."
    )
    assert version == 1


def test_classify_lesson_relative_path_still_works(hook):
    """Relative paths (e.g. from tests) must still resolve correctly after P1 fix."""
    manifest = {
        "version": 1,
        "updated_at": "",
        "validated_core": [],
        "exempt": [],
        "holdout_population": ["patterns/persistent-learning"],
    }
    hook._policy_manifest_cache = manifest

    rel_path = "lessons/patterns/persistent-learning.md"
    policy_class, version = hook._classify_lesson(rel_path)

    hook._policy_manifest_cache = None

    assert policy_class == "holdout"
    assert version == 1


def test_classify_lesson_missing_manifest_returns_holdout(hook, tmp_path, monkeypatch):
    """P2 fix: when the manifest file is absent, _classify_lesson must return
    'holdout' (all lessons in holdout), not 'unknown'.

    Before the fix the default holdout_population was [] so every lesson came
    back as 'unknown', contradicting the docstring.
    """
    hook._policy_manifest_cache = None
    # Point manifest path to a non-existent file
    monkeypatch.setattr(
        hook, "_policy_manifest_path", lambda: tmp_path / "nonexistent.yaml"
    )

    policy_class, version = hook._classify_lesson("lessons/any/lesson.md")

    hook._policy_manifest_cache = None  # reset

    assert policy_class == "holdout", (
        f"Expected 'holdout' when manifest is missing, got {policy_class!r}. "
        "The safe default must treat all lessons as holdout, not unknown."
    )


def test_classify_lesson_dot_relative_path(hook):
    """P3 fix: _classify_lesson must handle dot-relative paths (./lessons/X/Y.md).

    str(Path("./lessons/foo.md")) yields "./lessons/foo.md", which neither
    contains "/lessons/" nor starts with "lessons/". Without the fix the else
    branch used the whole string as the key, so the manifest lookup always missed.
    """
    manifest = {
        "version": 1,
        "updated_at": "",
        "validated_core": [],
        "exempt": [],
        "holdout_population": ["patterns/persistent-learning"],
    }
    hook._policy_manifest_cache = manifest

    dot_path = "./lessons/patterns/persistent-learning.md"
    policy_class, version = hook._classify_lesson(dot_path)

    hook._policy_manifest_cache = None

    assert policy_class == "holdout", (
        f"Expected 'holdout' for dot-relative path {dot_path!r}, got {policy_class!r}. "
        "Path normalization must strip ./ before the lessons/ prefix."
    )
    assert version == 1


def test_classify_lesson_non_dict_yaml_does_not_raise(hook, tmp_path, monkeypatch):
    """P1 fix: _load_policy_manifest must not cache a non-dict YAML result.

    If manifest.yaml contains a valid YAML list (e.g. `- item`), yaml.safe_load
    returns a list. Without the isinstance guard, _classify_lesson would call
    list.get(...) and raise AttributeError, silently disabling dropout logging.
    """
    import yaml

    manifest_file = tmp_path / "manifest.yaml"
    manifest_file.write_text(yaml.dump(["item1", "item2"]))

    hook._policy_manifest_cache = None
    monkeypatch.setattr(hook, "_policy_manifest_path", lambda: manifest_file)

    # Must not raise; a non-dict result should fall back to the empty-manifest default
    policy_class, version = hook._classify_lesson("lessons/any/lesson.md")

    hook._policy_manifest_cache = None

    # Empty fallback manifest has no holdout_population key → default [] → "unknown"
    assert policy_class in (
        "holdout",
        "unknown",
    ), f"Expected 'holdout' or 'unknown' for non-dict manifest, got {policy_class!r}."


def test_classify_lesson_present_manifest_missing_holdout_key_returns_unknown(hook):
    """P2 fix: a present manifest without holdout_population must yield 'unknown'.

    The '*' sentinel default was meant only for the missing/unparseable manifest
    branches. For a present manifest that simply lacks the key, the correct
    behaviour is 'unknown' — the lesson exists but isn't catalogued yet.
    """
    manifest = {
        "version": 2,
        "updated_at": "2026-08-18T00:00:00Z",
        "validated_core": [],
        "exempt": [],
        # holdout_population intentionally absent
    }
    hook._policy_manifest_cache = manifest

    policy_class, version = hook._classify_lesson("lessons/new/uncatalogued-lesson.md")

    hook._policy_manifest_cache = None

    assert policy_class == "unknown", (
        f"Expected 'unknown' for present manifest with no holdout_population key, "
        f"got {policy_class!r}. Unlisted lessons in a present manifest should be "
        "'unknown', not spuriously 'holdout'."
    )
    assert version == 2


# --- metadata.status (Agent Skills lifecycle) ---


def _write_skill_md(tmp_path: Path, name: str, frontmatter: str) -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\n{frontmatter}---\n\n# {name}\n\nBody.\n")
    return path


def test_scan_lessons_skips_skill_with_only_nested_metadata_status(hook, tmp_path):
    """Agent Skills frontmatter has no top-level `status`, so a deprecated skill
    can only record it under `metadata.status`. It must still be filtered out."""
    lessons_dir = tmp_path / "skills"
    _write_skill_md(
        lessons_dir,
        "lifecycle-phase-index",
        'name: lifecycle-phase-index\ndescription: "Route work through phases."\nmetadata:\n  status: deprecated\n',
    )

    assert hook.scan_lessons([lessons_dir]) == []


def test_scan_lessons_keeps_skill_with_nested_active_status(hook, tmp_path):
    lessons_dir = tmp_path / "skills"
    path = _write_skill_md(
        lessons_dir,
        "still-live",
        'name: still-live\ndescription: "Do the live thing."\nmetadata:\n  status: active\n',
    )

    assert [entry["path"] for entry in hook.scan_lessons([lessons_dir])] == [str(path)]


def test_effective_status_prefers_non_active_from_either_location(hook):
    assert hook.effective_status({}) == "active"
    assert hook.effective_status({"status": "active"}) == "active"
    assert hook.effective_status({"metadata": {"status": "archived"}}) == "archived"
    # A stale top-level `active` must not mask a nested deprecation.
    assert (
        hook.effective_status(
            {"status": "active", "metadata": {"status": "deprecated"}}
        )
        == "deprecated"
    )
    # Non-dict metadata must not raise.
    assert hook.effective_status({"metadata": "not-a-dict"}) == "active"


def test_effective_status_non_string_fails_closed(hook):
    """Non-string status values must not be treated as active.

    Mirrors the gptme-rag matcher: only the exact string "active" enables
    injection, so `status: false` (bool) or `status: [deprecated]` (list) cannot
    silently re-enable a deprecated lesson or skill.
    """
    assert hook.effective_status({"status": False}) != "active"
    assert hook.effective_status({"status": ["deprecated"]}) != "active"
    assert hook.effective_status({"metadata": {"status": False}}) != "active"
    assert hook.effective_status({"status": ""}) == "active"
    assert hook.effective_status({"status": "  "}) == "active"


def test_extract_frontmatter_regex_fallback_sees_nested_status(hook, monkeypatch):
    """Without PyYAML the regex fallback must still see a nested deprecation,
    even when an earlier top-level `status: active` precedes it."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    fm, _ = hook.extract_frontmatter(
        "---\nstatus: active\nmetadata:\n  status: deprecated\n---\n# Title\n"
    )

    assert hook.effective_status(fm) == "deprecated"


def test_extract_frontmatter_regex_fallback_ignores_status_in_comments(
    hook, monkeypatch
):
    """The regex fallback must not pick up `status: deprecated` from YAML comments
    or from a description field value — only real YAML key lines count."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # A comment containing `status: deprecated` must not override the real active status.
    fm, _ = hook.extract_frontmatter(
        "---\nstatus: active\n# status: deprecated\n---\n# Title\n"
    )
    assert (
        hook.effective_status(fm) == "active"
    ), "comment line must not override real status"

    # A description value mentioning `status: deprecated` must not override active.
    fm2, _ = hook.extract_frontmatter(
        "---\nstatus: active\ndescription: Fix status: deprecated handling\n---\n# Title\n"
    )
    assert (
        hook.effective_status(fm2) == "active"
    ), "description text must not override real status"


def test_extract_frontmatter_regex_fallback_ignores_status_in_block_scalar(
    hook, monkeypatch
):
    """The regex fallback must not be fooled by `status: deprecated` that appears
    as a content line inside a YAML block scalar (e.g. `description: |`).

    Block scalar content lines are indented, so `^[^\\S\\n]*status:` would match
    them even though they are not YAML keys.  The fix scopes the indented lookup
    to the `metadata:` block body only, so a block scalar sibling is invisible.
    """
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # A block scalar where the content happens to contain `status: deprecated`
    # on its own line.  The lesson itself is `status: active`.
    frontmatter = (
        "---\n"
        "status: active\n"
        "description: |\n"
        "  This lesson explains status: deprecated handling.\n"
        "  status: deprecated lessons are skipped by the matcher.\n"
        "---\n"
        "# Title\n"
    )
    fm, _ = hook.extract_frontmatter(frontmatter)
    assert (
        hook.effective_status(fm) == "active"
    ), "block scalar content must not override the real top-level status"


def test_extract_frontmatter_regex_fallback_non_word_status_fails_closed(
    hook, monkeypatch
):
    """Regex fallback: `status: [deprecated]` (list value) must not be treated as active.

    The ``\\w+`` pattern only captures word characters, so ``[deprecated]`` was
    silently dropped — leaving the status absent from fm and effective_status()
    returning "active".  The fix uses ``\\S+`` to capture any non-whitespace value
    so the list syntax reaches effective_status() as a non-active string.
    """
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    fm, _ = hook.extract_frontmatter("---\nstatus: [deprecated]\n---\n# Title\n")
    assert (
        hook.effective_status(fm) != "active"
    ), "status: [deprecated] in regex fallback must not be treated as active"

    fm2, _ = hook.extract_frontmatter(
        "---\nmetadata:\n  status: [deprecated]\n---\n# Title\n"
    )
    assert (
        hook.effective_status(fm2) != "active"
    ), "metadata.status: [deprecated] in regex fallback must not be treated as active"


def test_extract_frontmatter_regex_fallback_quoted_active_not_dropped(
    hook, monkeypatch
):
    """Regex fallback: ``status: "active"`` (quoted) must still inject the lesson.

    ``\\S+`` captures surrounding quotes, so the raw captured value is ``"active"``
    (with quotes) instead of ``active``.  Without quote-stripping the comparison
    ``s != "active"`` evaluates true and the lesson is silently dropped — a
    regression vs. the prior ``\\w+`` pattern which simply never matched quoted
    values and left status absent (defaulting to active).

    The fix strips ``"`` and ``'`` from captured values before comparison.
    """
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    fm, _ = hook.extract_frontmatter('---\nstatus: "active"\n---\n# Title\n')
    assert (
        hook.effective_status(fm) == "active"
    ), 'status: "active" (quoted) must not be silently dropped by regex fallback'

    fm2, _ = hook.extract_frontmatter(
        "---\nmetadata:\n  status: 'active'\n---\n# Title\n"
    )
    assert (
        hook.effective_status(fm2) == "active"
    ), "metadata.status: 'active' (single-quoted) must not be silently dropped"


# --- Class-aware dropout: differential epsilon (Phase 1) ---


def test_cc_dropout_epsilon_global_non_finite(hook, monkeypatch):
    """Non-finite LESSON_DROPOUT_EPSILON (inf, -inf, nan) must return 0.0, matching
    the validated_core helper's handling — prevents accidental 100% dropout from a
    mis-set env var (e.g. LESSON_DROPOUT_EPSILON=inf)."""
    for non_finite in ("nan", "inf", "-inf"):
        monkeypatch.setenv("LESSON_DROPOUT_EPSILON", non_finite)
        result = hook._get_dropout_epsilon()
        assert (
            result == 0.0
        ), f"Expected 0.0 for LESSON_DROPOUT_EPSILON={non_finite!r}, got {result}"


def test_cc_dropout_epsilon_validated_core_default(hook, monkeypatch):
    monkeypatch.delenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", raising=False)
    assert hook._get_dropout_epsilon_validated_core() == 0.05


def test_cc_dropout_epsilon_validated_core_env_override(hook, monkeypatch):
    monkeypatch.setenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", "0.10")
    assert hook._get_dropout_epsilon_validated_core() == 0.10
    monkeypatch.setenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", "0.0")
    assert hook._get_dropout_epsilon_validated_core() == 0.0
    monkeypatch.setenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", "2.5")
    assert hook._get_dropout_epsilon_validated_core() == 1.0
    monkeypatch.setenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", "not-a-float")
    assert hook._get_dropout_epsilon_validated_core() == 0.05  # fallback to default
    for non_finite in ("nan", "inf", "-inf"):
        monkeypatch.setenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", non_finite)
        assert hook._get_dropout_epsilon_validated_core() == 0.05


def test_cc_dropout_epsilon_for_class(hook, monkeypatch):
    monkeypatch.delenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", raising=False)
    # exempt is always 0
    assert hook._get_dropout_epsilon_for_class("exempt", 0.20) == 0.0
    assert hook._get_dropout_epsilon_for_class("exempt", 1.0) == 0.0
    # validated_core uses lower env-controlled epsilon (default 0.05)
    eff = hook._get_dropout_epsilon_for_class("validated_core", 0.20)
    assert eff == 0.05
    assert eff < 0.20
    # holdout/unknown use global epsilon
    assert hook._get_dropout_epsilon_for_class("holdout", 0.20) == 0.20
    assert hook._get_dropout_epsilon_for_class("unknown", 0.15) == 0.15


def test_cc_dropout_epsilon_for_class_global_zero_disables_validated_core(
    hook, monkeypatch
):
    """Global epsilon=0 (documented no-op default) must disable validated_core
    dropout too, not just holdout/unknown — validated_core must not use its own
    (nonzero-by-default) epsilon while the global switch is off."""
    monkeypatch.delenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", raising=False)
    assert hook._get_dropout_epsilon_for_class("validated_core", 0.0) == 0.0
    assert hook._get_dropout_epsilon_for_class("holdout", 0.0) == 0.0
    assert hook._get_dropout_epsilon_for_class("unknown", 0.0) == 0.0
    # exempt stays 0 regardless
    assert hook._get_dropout_epsilon_for_class("exempt", 0.0) == 0.0


def test_cc_dropout_epsilon_for_class_validated_core_bounded_by_global(
    hook, monkeypatch
):
    """validated_core epsilon must never exceed the global epsilon — otherwise
    a small global dropout (meant to be gentle) would withhold the lessons the
    feature is meant to preserve at a HIGHER rate than ordinary lessons."""
    monkeypatch.setenv("LESSON_DROPOUT_EPSILON_VALIDATED_CORE", "0.05")
    # global epsilon (0.01) is smaller than the validated_core default (0.05):
    # the effective rate must be clamped down to the global epsilon.
    assert hook._get_dropout_epsilon_for_class("validated_core", 0.01) == 0.01
    # global epsilon (0.20) is larger than validated_core (0.05): the smaller
    # class-specific rate wins, as before.
    assert hook._get_dropout_epsilon_for_class("validated_core", 0.20) == 0.05


def test_cc_dropout_exempt_lesson_never_withheld(hook, monkeypatch, tmp_path):
    """An exempt lesson must never be withheld even at epsilon=1.0 in the CC hook."""
    import random as _random

    log_dir = tmp_path / "drop"
    exempt_path = tmp_path / "lessons" / "exempt" / "my-exempt-lesson.md"

    # Inject a manifest and use the absolute paths produced by scan_lessons.
    hook._policy_manifest_cache = {
        "version": 1,
        "validated_core": [],
        "exempt": ["exempt/my-exempt-lesson"],
        "holdout_population": [],
    }
    try:
        monkeypatch.setenv("LESSON_DROPOUT_EPSILON", "1.0")
        monkeypatch.setenv("LESSON_DROPOUT_LOG_DIR", str(log_dir))
        monkeypatch.setenv("GPTME_SESSION_ID", "sess-cc-exempt")

        _random.seed(42)
        matches = [{"path": str(exempt_path), "title": "Exempt Lesson"}]
        predicted: list = []
        kept_m, kept_p = hook._apply_lesson_dropout_multi(
            matches, predicted, "sess-cc-exempt", tmp_path
        )
        assert len(kept_m) == 1, "Exempt lesson was withheld — should be kept"
        assert kept_m[0]["path"] == str(exempt_path)
    finally:
        hook._policy_manifest_cache = None


@pytest.mark.parametrize("failure", ["malformed", "unreadable"])
def test_cc_dropout_unavailable_manifest_skips_dropout(
    hook, monkeypatch, tmp_path, failure
):
    """A broken policy manifest activates the '*' sentinel, which must skip dropout
    entirely to preserve the exempt guarantee: when classification is unreliable,
    no lesson is withheld — not even lessons that would otherwise be holdout."""
    manifest_file = tmp_path / "manifest.yaml"
    manifest_file.write_text("[" if failure == "malformed" else "{}", encoding="utf-8")
    hook._policy_manifest_cache = None
    monkeypatch.setattr(hook, "_policy_manifest_path", lambda: manifest_file)
    if failure == "unreadable":
        real_open = open

        def unreadable_manifest(path, *args, **kwargs):
            if path == manifest_file:
                raise PermissionError("manifest is unreadable")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", unreadable_manifest)
    monkeypatch.setenv("LESSON_DROPOUT_EPSILON", "1.0")
    monkeypatch.setenv("LESSON_DROPOUT_LOG_DIR", str(tmp_path / "drop"))

    matches = [{"path": str(tmp_path / "lessons" / "broken.md"), "title": "Broken"}]
    kept_m, kept_p = hook._apply_lesson_dropout_multi(
        matches, [], f"sess-cc-{failure}", tmp_path
    )

    # Both failure modes activate the "*" sentinel → dropout is skipped, all lessons kept.
    assert kept_m == matches, "Lessons should be kept when manifest is unavailable"
    assert kept_p == []


def test_cc_dropout_withheld_has_effective_epsilon(hook, monkeypatch, tmp_path):
    """Withheld records in CC hook must include effective_epsilon."""
    import json
    import random as _random

    log_dir = tmp_path / "drop"
    monkeypatch.setenv("LESSON_DROPOUT_EPSILON", "1.0")
    monkeypatch.setenv("LESSON_DROPOUT_LOG_DIR", str(log_dir))
    monkeypatch.setenv("GPTME_SESSION_ID", "sess-cc-eff-eps")

    # Provide a real manifest (no "*" sentinel) so dropout is not suppressed.
    # Without a manifest, the sentinel path skips dropout to preserve exempt safety.
    hook._policy_manifest_cache = {
        "version": 1,
        "validated_core": [],
        "exempt": [],
        "holdout_population": [],  # real manifest, no "*" sentinel
    }
    _random.seed(0)
    matches = [{"path": "lessons/holdout/my-lesson.md", "title": "Holdout"}]
    try:
        hook._apply_lesson_dropout_multi(matches, [], "sess-cc-eff-eps", tmp_path)
    finally:
        hook._policy_manifest_cache = None

    log_file = log_dir / "sess-cc-eff-eps.jsonl"
    records = [json.loads(line) for line in log_file.read_text().splitlines() if line]
    assert records, "No log written"
    withheld = records[0]["withheld"]
    assert withheld, "No withheld lessons"
    assert (
        "effective_epsilon" in withheld[0]
    ), "effective_epsilon missing from withheld record in CC hook"


# --- format_lessons: SKILL.md pointer-injection on PreToolUse ---


def _skill_match(
    path: str, title: str = "computer-use", body: str = "FULL BODY"
) -> dict:
    return {
        "path": path,
        "title": title,
        "body": body,
        "description": "Use when you need to take screenshots or test web UIs.",
    }


def _lesson_match(
    path: str, title: str = "merge-conflicts", body: str = "LESSON BODY"
) -> dict:
    return {
        "path": path,
        "title": title,
        "body": body,
        "description": "Resolve merge conflicts cleanly.",
    }


def test_format_lessons_pretool_skill_renders_pointer_not_body(hook):
    """PreToolUse SKILL.md matches render as a compact pointer, not the full body."""
    skill = _skill_match("/workspace/skills/computer-use/SKILL.md")
    context = hook.format_lessons([skill], set(), event_type="PreToolUse")
    assert "FULL BODY" not in context
    assert "skill pointer" in context
    assert "computer-use" in context
    assert "Use when you need to take screenshots" in context
    assert "skills/computer-use/SKILL.md" in context
    assert "Read the full skill" in context


def test_format_lessons_userprompt_skill_keeps_full_body(hook):
    """UserPromptSubmit keeps full-body injection for SKILL.md matches."""
    skill = _skill_match("/workspace/skills/computer-use/SKILL.md")
    context = hook.format_lessons([skill], set(), event_type="UserPromptSubmit")
    assert "FULL BODY" in context
    assert "skill pointer" not in context


def test_format_lessons_pretool_lesson_keeps_full_body(hook):
    """PreToolUse keeps full-body injection for regular lessons (small by design)."""
    lesson = _lesson_match("/workspace/lessons/workflow/merge-conflicts.md")
    context = hook.format_lessons([lesson], set(), event_type="PreToolUse")
    assert "LESSON BODY" in context
    assert "skill pointer" not in context


def test_format_lessons_default_event_keeps_full_body(hook):
    """Default event_type (UserPromptSubmit) keeps full-body for backward compat."""
    skill = _skill_match("/workspace/skills/computer-use/SKILL.md")
    context = hook.format_lessons([skill], set())
    assert "FULL BODY" in context
    assert "skill pointer" not in context
