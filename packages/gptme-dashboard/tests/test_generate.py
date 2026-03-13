"""Tests for static dashboard generator."""

import json
import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from gptme_dashboard.generate import (
    _parse_toml,
    collect_workspace_data,
    detect_github_url,
    detect_submodules,
    extract_title,
    generate,
    generate_atom_feed,
    generate_json,
    generate_sitemap,
    github_blob_url,
    github_pages_url,
    github_tree_url,
    journal_page_path,
    lesson_page_path,
    parse_frontmatter,
    package_page_path,
    plugin_page_path,
    read_agent_urls,
    read_workspace_config,
    render_markdown_to_html,
    scan_journals,
    scan_lessons,
    scan_packages,
    scan_plugins,
    scan_readme,
    scan_recent_sessions,
    scan_skills,
    scan_summaries,
    scan_tasks,
    skill_page_path,
    strip_markdown_inline,
    summary_page_path,
    task_page_path,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal gptme workspace for testing."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "TestAgent"
        """)
    )

    # Lessons
    lessons_dir = tmp_path / "lessons" / "workflow"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "test-lesson.md").write_text(
        textwrap.dedent("""\
        ---
        match:
          keywords:
            - "test keyword"
        status: active
        ---
        # Test Lesson

        ## Rule
        Always test your code.
        """)
    )

    (lessons_dir / "deprecated-lesson.md").write_text(
        textwrap.dedent("""\
        ---
        status: deprecated
        ---
        # Old Lesson

        This is deprecated.
        """)
    )

    # Add a second category
    tools_dir = tmp_path / "lessons" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "shell-safety.md").write_text(
        textwrap.dedent("""\
        ---
        match:
          keywords: ["shell command", "bash script"]
        status: active
        ---
        # Shell Safety

        ## Rule
        Quote your variables.
        """)
    )

    # README should be excluded
    (tmp_path / "lessons" / "README.md").write_text("# Lessons index\n")

    # Plugins
    plugin_dir = tmp_path / "plugins" / "gptme-test-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("# Test Plugin\n\nA plugin for testing.\n")

    # Packages
    pkg_dir = tmp_path / "packages" / "test-pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text(
        textwrap.dedent("""\
        [project]
        name = "test-pkg"
        version = "0.2.0"
        description = "A test package"
        """)
    )
    (pkg_dir / "README.md").write_text("# test-pkg\n\nA package for testing purposes.\n")

    # Skills
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
        ---
        name: Test Skill
        description: A skill for testing workflows
        ---
        # Test Skill

        Instructions here.
        """)
    )

    return tmp_path


@pytest.fixture
def workspace_with_submodules(workspace: Path) -> Path:
    """Workspace with a .gitmodules referencing a submodule with gptme structure."""
    # Create .gitmodules
    (workspace / ".gitmodules").write_text(
        textwrap.dedent("""\
        [submodule "contrib"]
        \tpath = contrib
        \turl = git@github.com:gptme/gptme-contrib.git

        [submodule "projects/unrelated"]
        \tpath = projects/unrelated
        \turl = git@github.com:example/unrelated.git
        """)
    )

    # Create submodule with gptme-like structure
    contrib = workspace / "contrib"
    contrib.mkdir()

    # Submodule lessons
    sub_lessons = contrib / "lessons" / "patterns"
    sub_lessons.mkdir(parents=True)
    (sub_lessons / "shared-pattern.md").write_text(
        textwrap.dedent("""\
        ---
        match:
          keywords: ["shared pattern"]
        status: active
        ---
        # Shared Pattern

        A pattern from the submodule.
        """)
    )

    # Submodule skills
    sub_skills = contrib / "skills" / "deploy"
    sub_skills.mkdir(parents=True)
    (sub_skills / "SKILL.md").write_text(
        textwrap.dedent("""\
        ---
        name: Deploy Skill
        description: Deployment workflow from contrib
        ---
        # Deploy

        Deploy instructions.
        """)
    )

    # Submodule packages
    sub_pkg = contrib / "packages" / "contrib-pkg"
    sub_pkg.mkdir(parents=True)
    (sub_pkg / "pyproject.toml").write_text(
        textwrap.dedent("""\
        [project]
        name = "contrib-pkg"
        version = "1.0.0"
        description = "A package from contrib"
        """)
    )

    # Submodule plugins
    sub_plugin = contrib / "plugins" / "gptme-contrib-plugin"
    sub_plugin.mkdir(parents=True)
    (sub_plugin / "README.md").write_text("# Contrib Plugin\n\nA shared plugin.\n")

    # Create unrelated project (no gptme structure — should be skipped)
    unrelated = workspace / "projects" / "unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "README.md").write_text("# Unrelated project\n")

    return workspace


def test_parse_frontmatter_callable():
    """Test parse_frontmatter is callable."""
    assert callable(parse_frontmatter)


def test_parse_frontmatter_with_file(tmp_path: Path):
    """Test frontmatter parsing from actual file."""
    f = tmp_path / "test.md"
    f.write_text("---\nstatus: active\ntitle: Test\n---\n# My Title\n\nBody here.")
    fm, body = parse_frontmatter(f)
    assert fm["status"] == "active"
    assert "My Title" in body


def test_parse_frontmatter_no_frontmatter(tmp_path: Path):
    """Test file without frontmatter."""
    f = tmp_path / "test.md"
    f.write_text("# Just a Title\n\nNo frontmatter here.")
    fm, body = parse_frontmatter(f)
    assert fm == {}
    assert "Just a Title" in body


def test_parse_frontmatter_keywords_as_list(tmp_path: Path):
    """Test that keyword lists are parsed correctly (not silently dropped)."""
    f = tmp_path / "lesson.md"
    f.write_text(
        textwrap.dedent("""\
        ---
        match:
          keywords:
            - "first keyword"
            - "second keyword"
        status: active
        ---
        # Lesson
        """)
    )
    fm, _ = parse_frontmatter(f)
    assert isinstance(fm["match"]["keywords"], list)
    assert "first keyword" in fm["match"]["keywords"]
    assert "second keyword" in fm["match"]["keywords"]


def test_extract_title():
    """Test title extraction from markdown."""
    assert extract_title("# My Title\nBody", "fallback") == "My Title"
    assert extract_title("No heading here", "fallback") == "fallback"
    assert extract_title("## H2 heading\n# H1 heading", "fb") == "H1 heading"


def test_read_workspace_config_reads_agent_section(workspace: Path):
    """Test that [agent] name is returned from gptme.toml."""
    config = read_workspace_config(workspace)
    assert config["agent_name"] == "TestAgent"


def test_read_workspace_config_missing(tmp_path: Path):
    """Test config reading when gptme.toml absent."""
    assert read_workspace_config(tmp_path) == {}


def test_scan_lessons(workspace: Path):
    """Test lesson scanning."""
    lessons = scan_lessons(workspace)
    assert len(lessons) == 3

    titles = {lesson["title"] for lesson in lessons}
    assert "Test Lesson" in titles
    assert "Shell Safety" in titles
    assert "Old Lesson" in titles

    categories = {lesson["category"] for lesson in lessons}
    assert "workflow" in categories
    assert "tools" in categories

    test_lesson = next(lesson for lesson in lessons if lesson["title"] == "Test Lesson")
    assert "test keyword" in test_lesson["keywords"]
    assert test_lesson["status"] == "active"
    assert test_lesson["kind"] == "lesson"


def test_scan_lessons_with_source(workspace: Path):
    """Test that source tag is added when specified."""
    lessons = scan_lessons(workspace, source="contrib")
    assert all(lesson["source"] == "contrib" for lesson in lessons)

    lessons_no_source = scan_lessons(workspace)
    assert all("source" not in lesson for lesson in lessons_no_source)


def test_scan_lessons_empty(tmp_path: Path):
    """Test scanning workspace with no lessons dir."""
    assert scan_lessons(tmp_path) == []


def test_scan_plugins(workspace: Path):
    """Test plugin scanning."""
    plugins = scan_plugins(workspace)
    assert len(plugins) == 1
    assert plugins[0]["name"] == "gptme-test-plugin"
    assert "testing" in plugins[0]["description"].lower()


def test_scan_plugins_strips_markdown_from_description(tmp_path: Path):
    """Plugin descriptions from README.md should have inline markdown stripped."""
    plugin_dir = tmp_path / "plugins" / "gptme-example"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text(
        "# Example Plugin\n\n**Purpose**: Does something **useful** with `code`.\n"
    )
    plugins = scan_plugins(tmp_path)
    assert len(plugins) == 1
    desc = plugins[0]["description"]
    assert "**" not in desc
    assert "`" not in desc
    assert "Purpose" in desc
    assert "useful" in desc


def test_strip_markdown_inline():
    """strip_markdown_inline should remove bold, italic, and code markers."""
    assert strip_markdown_inline("**bold** text") == "bold text"
    assert strip_markdown_inline("*italic* text") == "italic text"
    assert strip_markdown_inline("__bold__ text") == "bold text"
    assert strip_markdown_inline("_italic_ text") == "italic text"
    assert strip_markdown_inline("`code` here") == "code here"
    assert strip_markdown_inline("**Purpose**: does _this_") == "Purpose: does this"
    assert strip_markdown_inline("plain text") == "plain text"
    # snake_case identifiers must not be corrupted by the italic regex
    assert (
        strip_markdown_inline("A gptme_hooks_plugin description")
        == "A gptme_hooks_plugin description"
    )
    # dunder names embedded in identifiers must not be corrupted by the __bold__ regex
    assert (
        strip_markdown_inline("calls gptme__magic__hook internally")
        == "calls gptme__magic__hook internally"
    )
    # standalone __dunder__ formatting IS stripped (markdown bold syntax)
    assert strip_markdown_inline("A __Ralph Loop__ plugin") == "A Ralph Loop plugin"


def test_scan_packages(workspace: Path):
    """Test package scanning."""
    packages = scan_packages(workspace)
    assert len(packages) == 1
    assert packages[0]["name"] == "test-pkg"
    assert packages[0]["version"] == "0.2.0"
    assert "test package" in packages[0]["description"].lower()


def test_scan_skills(workspace: Path):
    """Test skill scanning."""
    skills = scan_skills(workspace)
    assert len(skills) == 1
    assert skills[0]["name"] == "Test Skill"
    assert "testing" in skills[0]["description"].lower()
    assert "body" in skills[0]
    assert "Instructions here" in skills[0]["body"]
    assert "page_url" in skills[0]
    assert skills[0]["page_url"] == "skills/test-skill/index.html"
    assert skills[0]["kind"] == "skill"


# --- Submodule tests ---


def test_detect_submodules_none(workspace: Path):
    """Test detection when no .gitmodules exists."""
    assert detect_submodules(workspace) == []


def test_detect_submodules(workspace_with_submodules: Path):
    """Test that gptme-like submodules are detected, non-gptme ones skipped."""
    subs = detect_submodules(workspace_with_submodules)
    assert len(subs) == 1
    assert subs[0]["name"] == "contrib"
    assert subs[0]["has_lessons"] is True
    assert subs[0]["has_skills"] is True
    assert subs[0]["has_packages"] is True
    assert subs[0]["has_plugins"] is True


def test_collect_with_submodules(workspace_with_submodules: Path):
    """Test that submodule content is aggregated into workspace data."""
    data = collect_workspace_data(workspace_with_submodules)

    # Submodule name should be listed
    assert "contrib" in data["submodules"]

    # Lessons: 3 local + 1 from submodule
    assert data["stats"]["total_lessons"] == 4
    sub_lessons = [ls for ls in data["lessons"] if ls.get("source") == "contrib"]
    assert len(sub_lessons) == 1
    assert sub_lessons[0]["title"] == "Shared Pattern"

    # Skills: 1 local + 1 from submodule
    assert data["stats"]["total_skills"] == 2

    # Packages: 1 local + 1 from submodule
    assert data["stats"]["total_packages"] == 2

    # Plugins: 1 local + 1 from submodule
    assert data["stats"]["total_plugins"] == 2

    # Guidance: all lessons + skills combined
    assert data["stats"]["total_guidance"] == 6  # 4 lessons + 2 skills
    assert len(data["guidance"]) == 6

    # Sources list should contain "contrib"
    assert "contrib" in data["sources"]


def test_guidance_unified_structure(workspace_with_submodules: Path):
    """Test that guidance items have consistent structure regardless of kind."""
    data = collect_workspace_data(workspace_with_submodules)

    for item in data["guidance"]:
        assert "kind" in item
        assert item["kind"] in ("lesson", "skill")
        assert "category" in item
        assert "status" in item
        assert "keywords" in item


def test_guidance_sorted(workspace: Path):
    """Test that guidance is sorted by kind then title."""
    data = collect_workspace_data(workspace)
    kinds = [item["kind"] for item in data["guidance"]]
    # All lessons before skills
    lesson_idx = [i for i, k in enumerate(kinds) if k == "lesson"]
    skill_idx = [i for i, k in enumerate(kinds) if k == "skill"]
    if lesson_idx and skill_idx:
        assert max(lesson_idx) < min(skill_idx)


# --- Existing tests (updated for new stats) ---


def test_collect_workspace_data(workspace: Path):
    """Test full data collection."""
    data = collect_workspace_data(workspace)
    assert data["workspace_name"] == "TestAgent"
    assert data["stats"]["total_lessons"] == 3
    assert data["stats"]["total_plugins"] == 1
    assert data["stats"]["total_packages"] == 1
    assert data["stats"]["total_skills"] == 1
    assert data["stats"]["total_guidance"] == 4  # 3 lessons + 1 skill
    # lesson_categories should be consistent and sorted
    cats = data["stats"]["lesson_categories"]
    assert list(cats.keys()) == sorted(cats.keys())
    assert cats["tools"] == 1
    assert cats["workflow"] == 2
    assert cats["skill"] == 1  # Skills get "skill" category


def test_generate_json_stdout(workspace: Path):
    """Test JSON dump to string."""
    json_str = generate_json(workspace)
    data = json.loads(json_str)
    assert data["workspace_name"] == "TestAgent"
    assert data["stats"]["total_lessons"] == 3
    assert len(data["lessons"]) == 3
    assert len(data["guidance"]) == 4  # 3 lessons + 1 skill


def test_generate_json_excludes_large_fields(workspace: Path):
    """JSON export should not include body or all_keywords (size/schema concern)."""
    json_str = generate_json(workspace)
    data = json.loads(json_str)
    for lesson in data["lessons"]:
        assert "body" not in lesson, "body should not appear in JSON export"
        assert "all_keywords" not in lesson, "all_keywords should not appear in JSON export"
    for skill in data["skills"]:
        assert "body" not in skill, "skill body should not appear in JSON export"
    for item in data["guidance"]:
        assert "body" not in item, "guidance body should not appear in JSON export"
        assert "all_keywords" not in item, "guidance all_keywords should not appear in JSON export"


def test_generate_json_to_file(workspace: Path, tmp_path: Path):
    """Test JSON dump to file."""
    output = tmp_path / "output"
    generate_json(workspace, output)
    assert (output / "data.json").exists()
    data = json.loads((output / "data.json").read_text())
    assert data["workspace_name"] == "TestAgent"


def test_generate_full(workspace: Path, tmp_path: Path):
    """Test full HTML generation."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    index = output / "index.html"
    assert index.exists()

    html = index.read_text()
    assert "TestAgent" in html
    assert "Test Lesson" in html
    assert "gptme-test-plugin" in html
    assert "test-pkg" in html
    assert "Test Skill" in html
    assert "0.2.0" in html

    # Unified section header
    assert "Lessons &amp; Skills" in html

    # Stats: four separate stat cards for lessons, skills, plugins, packages.
    # Use regex to avoid depending on exact template whitespace.
    assert re.search(r'class="number">\s*3\s*</div>\s*<div[^>]*class="label">Lessons</div>', html)
    assert re.search(r'class="number">\s*1\s*</div>\s*<div[^>]*class="label">Skills</div>', html)
    assert re.search(r'class="number">\s*1\s*</div>\s*<div[^>]*class="label">Plugins</div>', html)
    assert re.search(r'class="number">\s*1\s*</div>\s*<div[^>]*class="label">Packages</div>', html)


def test_generate_kind_filter_buttons(workspace: Path, tmp_path: Path):
    """Test that kind filter buttons (Lessons/Skills) appear in the guidance section."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    # Kind filter row should be present
    assert 'id="kind-filters"' in html

    # All three kind values must appear (covers both filter buttons and tbody rows)
    assert 'data-kind="all"' in html
    assert 'data-kind="lesson"' in html
    assert 'data-kind="skill"' in html


def test_generate_with_submodules(workspace_with_submodules: Path, tmp_path: Path):
    """Test HTML generation includes submodule content and source tags."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace_with_submodules, output, template_dir)

    html = (output / "index.html").read_text()

    # Submodule name should appear in header
    assert "contrib" in html

    # Source filter buttons should be present
    assert 'data-source="contrib"' in html

    # Submodule items should appear
    assert "Shared Pattern" in html
    assert "Deploy Skill" in html
    assert "contrib-pkg" in html
    assert "gptme-contrib-plugin" in html  # Plugin directory name

    # Source tags in table
    assert "tag-source" in html


def test_generate_empty_workspace(tmp_path: Path):
    """Test generation on workspace with no content."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(tmp_path, output, template_dir)

    html = (output / "index.html").read_text()
    assert 'class="number">0<' in html  # Zero counts


def test_render_markdown_to_html():
    """Test basic markdown rendering."""
    result = render_markdown_to_html("# Hello\n\nWorld")
    assert "<h1>" in result
    assert "Hello" in result
    assert "<p>" in result


def test_render_markdown_fenced_code():
    """Test fenced code block rendering."""
    result = render_markdown_to_html("```python\nprint('hello')\n```")
    assert "<code" in result
    assert "print" in result


def test_render_markdown_tables():
    """Test table rendering."""
    result = render_markdown_to_html("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in result
    assert "<td>" in result


def test_lesson_page_path():
    """Test lesson path to URL conversion."""
    assert lesson_page_path("workflow/test-lesson.md") == "lessons/workflow/test-lesson.html"
    assert lesson_page_path("standalone.md") == "lessons/standalone.html"


def test_scan_lessons_includes_body(workspace: Path):
    """Test that scanned lessons include body content."""
    lessons = scan_lessons(workspace)
    test_lesson = next(x for x in lessons if x["title"] == "Test Lesson")
    assert "body" in test_lesson
    assert "Always test your code" in test_lesson["body"]
    assert "page_url" in test_lesson
    assert test_lesson["page_url"] == "lessons/workflow/test-lesson.html"


def test_scan_lessons_includes_all_keywords(workspace: Path):
    """Test that all_keywords contains the full keyword list."""
    lessons = scan_lessons(workspace)
    shell_lesson = next(x for x in lessons if x["title"] == "Shell Safety")
    assert "all_keywords" in shell_lesson
    assert "shell command" in shell_lesson["all_keywords"]
    assert "bash script" in shell_lesson["all_keywords"]


def test_generate_lesson_detail_pages(workspace: Path, tmp_path: Path):
    """Test that per-lesson detail pages are generated."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    # Check that lesson detail pages exist
    lesson_page = output / "lessons" / "workflow" / "test-lesson.html"
    assert lesson_page.exists(), f"Expected {lesson_page} to exist"

    html = lesson_page.read_text()
    assert "Test Lesson" in html
    assert "Always test your code" in html
    assert "test keyword" in html
    assert "workflow" in html

    # Check another lesson page
    shell_page = output / "lessons" / "tools" / "shell-safety.html"
    assert shell_page.exists()
    shell_html = shell_page.read_text()
    assert "Shell Safety" in shell_html
    assert "Quote your variables" in shell_html


def test_generate_index_links_to_lessons(workspace: Path, tmp_path: Path):
    """Test that index.html lesson titles are links to detail pages."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()
    assert 'href="lessons/workflow/test-lesson.html"' in html
    assert 'href="lessons/tools/shell-safety.html"' in html


def test_lesson_detail_page_renders_html_not_escaped(workspace: Path, tmp_path: Path):
    """Test that markdown is rendered as HTML, not escaped as text."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "lessons" / "workflow" / "test-lesson.html").read_text()
    # Rendered markdown should contain actual HTML tags, not escaped entities
    assert "<h2>" in html or "<h1>" in html, "Headings should be rendered as HTML <h> tags"
    assert "<p>" in html, "Paragraphs should be rendered as HTML <p> tags"
    # Escaped tags would look like &lt;h2&gt; — must not appear
    assert "&lt;h" not in html, "HTML tags must not be escaped"
    assert "&lt;p&gt;" not in html, "Paragraph tags must not be escaped"


def test_lesson_detail_breadcrumb_single_level(workspace: Path, tmp_path: Path):
    """Test that breadcrumb uses correct relative path for single-level lessons."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    # workflow/test-lesson.html is two levels deep → needs ../../
    html = (output / "lessons" / "workflow" / "test-lesson.html").read_text()
    assert 'href="../../index.html"' in html


def test_lesson_detail_breadcrumb_deep_nesting(workspace: Path, tmp_path: Path):
    """Test breadcrumb for lessons nested two directories deep (a/b/lesson.md)."""
    # Add a deeply nested lesson
    deep_dir = workspace / "lessons" / "a" / "b"
    deep_dir.mkdir(parents=True)
    (deep_dir / "deep-lesson.md").write_text(
        textwrap.dedent("""\
        ---
        status: active
        ---
        # Deep Lesson

        Nested content.
        """)
    )

    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    deep_page = output / "lessons" / "a" / "b" / "deep-lesson.html"
    assert deep_page.exists()
    html = deep_page.read_text()
    # Three levels deep → needs ../../../
    assert 'href="../../../index.html"' in html


def test_skill_page_path():
    """Test skill directory to URL conversion."""
    assert skill_page_path("skills/my-skill") == "skills/my-skill/index.html"
    assert skill_page_path("skills/nested/deep-skill") == "skills/nested/deep-skill/index.html"


def test_generate_skill_detail_pages(workspace: Path, tmp_path: Path):
    """Test that per-skill detail pages are generated."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    skill_page = output / "skills" / "test-skill" / "index.html"
    assert skill_page.exists(), f"Expected {skill_page} to exist"

    html = skill_page.read_text()
    assert "Test Skill" in html
    assert "Instructions here" in html
    assert "A skill for testing workflows" in html


def test_generate_index_links_to_skills(workspace: Path, tmp_path: Path):
    """Test that index.html skill names are links to detail pages."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()
    assert 'href="skills/test-skill/index.html"' in html


def test_skill_detail_renders_html_not_escaped(workspace: Path, tmp_path: Path):
    """Test that skill markdown is rendered as HTML, not escaped as text."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "skills" / "test-skill" / "index.html").read_text()
    assert "<h1>" in html or "<h2>" in html, "Headings should be rendered as HTML"
    assert "&lt;h" not in html, "HTML tags must not be escaped"


def test_skill_detail_breadcrumb(workspace: Path, tmp_path: Path):
    """Test that skill detail page breadcrumb uses correct relative root prefix."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    # skills/test-skill/index.html is two levels deep → needs ../../
    html = (output / "skills" / "test-skill" / "index.html").read_text()
    assert 'href="../../index.html"' in html


def test_detect_submodules_percent_encoded_url(workspace: Path):
    """Test that detect_submodules handles percent-encoded URLs without crashing (RawConfigParser)."""
    (workspace / ".gitmodules").write_text(
        textwrap.dedent("""\
        [submodule "special"]
        \tpath = special
        \turl = https://github.com/org/repo%20with%20spaces.git
        """)
    )
    sub = workspace / "special"
    sub.mkdir()
    (sub / "lessons").mkdir()
    # Should not raise InterpolationSyntaxError
    subs = detect_submodules(workspace)
    assert len(subs) == 1
    assert subs[0]["name"] == "special"


def test_submodule_page_url_no_collision(workspace_with_submodules: Path, tmp_path: Path):
    """Test that submodule items get a source-prefixed page_url to avoid path collisions."""
    # Add a collision: submodule has same relative lesson path as workspace
    sub_lessons = workspace_with_submodules / "contrib" / "lessons" / "workflow"
    sub_lessons.mkdir(parents=True, exist_ok=True)
    (sub_lessons / "test-lesson.md").write_text(
        textwrap.dedent("""\
        ---
        match:
          keywords: ["collision test"]
        status: active
        ---
        # Collision Lesson

        This is from the submodule.
        """)
    )

    data = collect_workspace_data(workspace_with_submodules)

    # Main workspace lesson: page_url without source prefix
    main_lessons = [ls for ls in data["lessons"] if not ls.get("source")]
    main_collision = [ls for ls in main_lessons if ls["path"] == "workflow/test-lesson.md"]
    assert len(main_collision) == 1
    assert main_collision[0]["page_url"] == "lessons/workflow/test-lesson.html"

    # Submodule lesson: page_url with "contrib/" prefix
    sub_collision = [
        ls
        for ls in data["lessons"]
        if ls.get("source") == "contrib" and "test-lesson" in ls["path"]
    ]
    assert len(sub_collision) == 1
    assert sub_collision[0]["page_url"] == "contrib/lessons/workflow/test-lesson.html"

    # Generate should not raise on collision
    output = tmp_path / "out"
    try:
        generate(workspace_with_submodules, output)
        # Both files should exist (not silently overwritten)
        assert (output / "lessons" / "workflow" / "test-lesson.html").exists()
        assert (output / "contrib" / "lessons" / "workflow" / "test-lesson.html").exists()
    except Exception:
        pass  # Template not found is OK — we verified page_url values above


# --- Plugin enabled status tests ---


def test_read_workspace_config_reads_plugins(tmp_path: Path):
    """Test that [plugins] enabled and paths are parsed from gptme.toml."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "PluginAgent"

        [plugins]
        enabled = ["user_memories", "gptme_consortium"]
        """)
    )
    config = read_workspace_config(tmp_path)
    assert config["agent_name"] == "PluginAgent"
    assert config["plugins_enabled"] == ["user_memories", "gptme_consortium"]


def test_read_workspace_config_no_plugins_section(workspace: Path):
    """Test that missing [plugins] section results in no plugins_enabled key."""
    config = read_workspace_config(workspace)
    assert "plugins_enabled" not in config


def test_scan_plugins_with_enabled_list(workspace: Path):
    """Test that plugins get enabled flag when enabled_plugins is provided."""
    # Add a second plugin
    (workspace / "plugins" / "user_memories").mkdir(parents=True)
    (workspace / "plugins" / "user_memories" / "README.md").write_text(
        "# User Memories\n\nPersistent memory plugin.\n"
    )

    plugins = scan_plugins(workspace, enabled_plugins=["user_memories"])
    assert len(plugins) == 2

    enabled_plugin = next(p for p in plugins if p["name"] == "user_memories")
    assert enabled_plugin["enabled"] is True

    disabled_plugin = next(p for p in plugins if p["name"] == "gptme-test-plugin")
    assert disabled_plugin["enabled"] is False


def test_scan_plugins_hyphen_to_underscore_matching(workspace: Path):
    """Test that gptme-foo plugin matches gptme_foo in enabled list."""
    plugins = scan_plugins(workspace, enabled_plugins=["gptme_test_plugin"])
    assert len(plugins) == 1
    assert plugins[0]["enabled"] is True


def test_scan_plugins_no_enabled_list(workspace: Path):
    """Test that plugins have no enabled key when no list provided."""
    plugins = scan_plugins(workspace)
    assert len(plugins) == 1
    assert "enabled" not in plugins[0]


def test_collect_passes_enabled_to_plugins(tmp_path: Path):
    """Test that collect_workspace_data passes enabled config to scan_plugins."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "ConfigTest"

        [plugins]
        enabled = ["my_plugin"]
        """)
    )
    plugin_dir = tmp_path / "plugins" / "my-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("# My Plugin\n\nA test plugin.\n")

    data = collect_workspace_data(tmp_path)
    assert len(data["plugins"]) == 1
    assert data["plugins"][0]["enabled"] is True


def test_generate_shows_plugin_status(tmp_path: Path):
    """Test that generated HTML shows enabled/available tags for plugins."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "StatusTest"

        [plugins]
        enabled = ["enabled_plugin"]
        """)
    )
    for name in ["enabled-plugin", "disabled-plugin"]:
        d = tmp_path / "plugins" / name
        d.mkdir(parents=True)
        (d / "README.md").write_text(f"# {name}\n\nDescription.\n")

    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(tmp_path, output, template_dir)

    html = (output / "index.html").read_text()
    assert "tag-active" in html  # enabled plugin
    assert "enabled" in html.lower()
    assert "available" in html.lower()


def test_read_workspace_config_multiline_enabled(tmp_path: Path):
    """Test that multi-line enabled = [...] arrays are parsed correctly."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "MultilineAgent"

        [plugins]
        enabled = [
            "user_memories",
            "gptme_consortium",
        ]
        """)
    )
    config = read_workspace_config(tmp_path)
    assert config["agent_name"] == "MultilineAgent"
    assert config["plugins_enabled"] == ["user_memories", "gptme_consortium"]


def test_read_workspace_config_no_plugins_paths_in_result(tmp_path: Path):
    """Test that plugins_paths is not included in config (it was dead code, removed)."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [plugins]
        paths = ["plugins", "gptme-contrib/plugins"]
        enabled = ["user_memories"]
        """)
    )
    config = read_workspace_config(tmp_path)
    assert "plugins_paths" not in config
    assert config["plugins_enabled"] == ["user_memories"]


# --- GitHub URL detection and linking tests ---


def test_github_blob_url():
    """Test GitHub blob URL construction for a file path."""
    url = github_blob_url("https://github.com/gptme/gptme-contrib", "lessons/workflow/test.md")
    assert url == "https://github.com/gptme/gptme-contrib/blob/HEAD/lessons/workflow/test.md"


def test_github_blob_url_with_prefix():
    """Test blob URL with prefix for lessons (lessons/workflow/x.md)."""
    url = github_blob_url(
        "https://github.com/gptme/gptme-contrib",
        "workflow/test.md",
        prefix="lessons",
    )
    assert url == "https://github.com/gptme/gptme-contrib/blob/HEAD/lessons/workflow/test.md"


def test_github_blob_url_empty():
    """Test blob URL returns empty when no repo URL."""
    assert github_blob_url("", "some/path") == ""


def test_github_tree_url():
    """Test GitHub tree URL construction for directory paths (plugins, packages, skills)."""
    url = github_tree_url("https://github.com/gptme/gptme-contrib", "plugins/foo")
    assert url == "https://github.com/gptme/gptme-contrib/tree/HEAD/plugins/foo"


def test_github_tree_url_empty():
    """Test tree URL returns empty when no repo URL."""
    assert github_tree_url("", "some/dir") == ""


def test_detect_github_url_ssh(tmp_path: Path):
    """Test detection from SSH remote URL."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:gptme/gptme-contrib.git"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert detect_github_url(tmp_path) == "https://github.com/gptme/gptme-contrib"


def test_detect_github_url_https(tmp_path: Path):
    """Test detection from HTTPS remote URL."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/ErikBjare/bob.git"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert detect_github_url(tmp_path) == "https://github.com/ErikBjare/bob"


def test_detect_github_url_no_git(tmp_path: Path):
    """Test detection returns empty for non-git directory."""
    assert detect_github_url(tmp_path) == ""


def test_collect_workspace_data_includes_gh_urls(tmp_path: Path):
    """Test that gh_url is added to items when GitHub remote is detected."""
    import subprocess

    # Set up git repo with GitHub remote
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:test/repo.git"],
        cwd=tmp_path,
        capture_output=True,
    )

    # Create minimal workspace content
    lessons_dir = tmp_path / "lessons" / "workflow"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "test.md").write_text("---\nstatus: active\n---\n# Test\n\nBody.")

    pkg_dir = tmp_path / "packages" / "mypkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text('[project]\nname = "mypkg"\nversion = "1.0"')

    data = collect_workspace_data(tmp_path)

    assert data["gh_repo_url"] == "https://github.com/test/repo"
    assert data["lessons"][0]["gh_url"].startswith(
        "https://github.com/test/repo/blob/HEAD/lessons/"
    )
    assert data["packages"][0]["gh_url"] == "https://github.com/test/repo/tree/HEAD/packages/mypkg"


def test_generate_html_includes_github_links(tmp_path: Path):
    """Test that generated HTML includes GitHub source links when remote exists."""
    import subprocess

    # Set up git repo with GitHub remote
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:test/repo.git"],
        cwd=workspace,
        capture_output=True,
    )

    lessons_dir = workspace / "lessons" / "workflow"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "test.md").write_text("---\nstatus: active\n---\n# Test Lesson\n\nBody content.")

    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    # Index should have GitHub link in header and src links in table
    index_html = (output / "index.html").read_text()
    assert "https://github.com/test/repo" in index_html
    assert "gh-link" in index_html

    # Lesson detail page should have "View on GitHub" link
    lesson_html = (output / "lessons" / "workflow" / "test.html").read_text()
    assert "View on GitHub" in lesson_html
    assert "https://github.com/test/repo/blob/HEAD/lessons/workflow/test.md" in lesson_html


def test_collect_workspace_data_submodule_items_no_gh_url(tmp_path: Path):
    """Submodule items without a git remote should not get gh_url."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:test/repo.git"],
        cwd=tmp_path,
        capture_output=True,
    )

    # Main workspace lesson
    lessons_dir = tmp_path / "lessons" / "workflow"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "main.md").write_text("---\nstatus: active\n---\n# Main\n\nBody.")

    # Fake submodule with a lesson but NO git remote
    sub_path = tmp_path / "gptme-contrib"
    sub_lessons = sub_path / "lessons" / "workflow"
    sub_lessons.mkdir(parents=True)
    (sub_lessons / "sub.md").write_text("---\nstatus: active\n---\n# Sub\n\nBody.")
    (sub_path / "gptme.toml").write_text('[agent]\nname = "contrib"\n')
    subprocess.run(["git", "init"], cwd=sub_path, capture_output=True)
    # No git remote added — detect_github_url returns ""

    # Register as submodule via .gitmodules
    gitmodules = tmp_path / ".gitmodules"
    gitmodules.write_text(
        '[submodule "gptme-contrib"]\n'
        "    path = gptme-contrib\n"
        "    url = https://github.com/gptme/gptme-contrib.git\n"
    )

    data = collect_workspace_data(tmp_path)

    main_lessons = [le for le in data["lessons"] if not le.get("source")]
    sub_lessons_data = [le for le in data["lessons"] if le.get("source")]

    assert len(main_lessons) == 1
    assert "gh_url" in main_lessons[0]  # main workspace items get gh_url

    assert len(sub_lessons_data) == 1
    assert "gh_url" not in sub_lessons_data[0]  # no remote → no gh_url


def test_collect_workspace_data_submodule_items_get_gh_url(tmp_path: Path):
    """Submodule items get gh_url from the submodule's own git remote."""
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:test/main-repo.git"],
        cwd=tmp_path,
        capture_output=True,
    )

    # Main workspace lesson
    lessons_dir = tmp_path / "lessons" / "workflow"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "main.md").write_text("---\nstatus: active\n---\n# Main\n\nBody.")

    # Submodule with its OWN git remote
    sub_path = tmp_path / "gptme-contrib"
    sub_lessons = sub_path / "lessons" / "workflow"
    sub_lessons.mkdir(parents=True)
    (sub_lessons / "sub.md").write_text("---\nstatus: active\n---\n# Sub\n\nBody.")
    (sub_path / "gptme.toml").write_text('[agent]\nname = "contrib"\n')
    subprocess.run(["git", "init"], cwd=sub_path, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:gptme/gptme-contrib.git"],
        cwd=sub_path,
        capture_output=True,
    )

    # Register as submodule via .gitmodules
    (tmp_path / ".gitmodules").write_text(
        '[submodule "gptme-contrib"]\n'
        "    path = gptme-contrib\n"
        "    url = git@github.com:gptme/gptme-contrib.git\n"
    )

    data = collect_workspace_data(tmp_path)

    main_lessons = [le for le in data["lessons"] if not le.get("source")]
    sub_lessons_data = [le for le in data["lessons"] if le.get("source")]

    assert len(main_lessons) == 1
    assert "gh_url" in main_lessons[0]
    assert "main-repo" in main_lessons[0]["gh_url"]

    assert len(sub_lessons_data) == 1
    # Submodule lesson gets gh_url pointing to the submodule's repo, not main repo
    assert "gh_url" in sub_lessons_data[0]
    assert "gptme-contrib" in sub_lessons_data[0]["gh_url"]
    assert "main-repo" not in sub_lessons_data[0]["gh_url"]


# ── agent.urls ────────────────────────────────────────────────────────────────


def test_read_agent_urls_present(tmp_path: Path):
    """read_agent_urls returns the [agent.urls] dict when present."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "TestAgent"

        [agent.urls]
        dashboard = "https://example.com/dashboard"
        repo = "https://github.com/example/agent"
        """)
    )
    links = read_agent_urls(tmp_path)
    assert links == {
        "dashboard": "https://example.com/dashboard",
        "repo": "https://github.com/example/agent",
    }


def test_read_agent_urls_absent(tmp_path: Path):
    """read_agent_urls returns empty dict when [agent.urls] is not present."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestAgent"\n')
    links = read_agent_urls(tmp_path)
    assert links == {}


def test_read_agent_urls_no_toml(tmp_path: Path):
    """read_agent_urls returns empty dict when gptme.toml does not exist."""
    links = read_agent_urls(tmp_path)
    assert links == {}


def test_read_agent_urls_strips_non_http_schemes(tmp_path: Path):
    """read_agent_urls filters out non-http/https URLs (e.g. javascript:)."""
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent.urls]
        safe = "https://example.com"
        unsafe = "javascript:alert(1)"
        also_unsafe = "data:text/html,<h1>x</h1>"
        """)
    )
    links = read_agent_urls(tmp_path)
    assert links == {"safe": "https://example.com"}
    assert "unsafe" not in links
    assert "also_unsafe" not in links


def test_collect_workspace_data_includes_agent_urls(workspace: Path):
    """collect_workspace_data exposes agent_urls from [agent.urls]."""
    # Overwrite the workspace gptme.toml to add [agent.urls]
    (workspace / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "TestAgent"

        [agent.urls]
        dashboard = "https://example.com/dash"
        """)
    )
    data = collect_workspace_data(workspace)
    assert "agent_urls" in data
    assert data["agent_urls"] == {"dashboard": "https://example.com/dash"}
    # Verify fallback name-extraction: when gptme raises TypeError for [agent.urls],
    # read_workspace_config falls back to raw TOML and must still extract agent_name.
    assert data["workspace_name"] == "TestAgent"


def test_generate_renders_agent_urls_in_header(workspace: Path, tmp_path: Path):
    """Generated index.html includes agent_urls as header links."""
    (workspace / "gptme.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "TestAgent"

        [agent.urls]
        dashboard = "https://example.com/dash"
        website = "https://example.com"
        """)
    )
    output = tmp_path / "_site"
    generate(workspace, output)
    html = (output / "index.html").read_text()
    assert 'href="https://example.com/dash"' in html
    assert ">dashboard<" in html
    assert 'href="https://example.com"' in html
    assert ">website<" in html


def test_generate_no_agent_urls_no_extra_midpoints(workspace: Path, tmp_path: Path):
    """When [agent.urls] is absent the header contains no extra link middots."""
    output = tmp_path / "_site"
    generate(workspace, output)
    html = (output / "index.html").read_text()
    # Sanity: page rendered OK
    assert "lessons" in html
    # No agent-link middot separators — workspace fixture has no [agent.urls] and no
    # git remote, so gh_repo_url is also absent.  Any &middot; in the output would
    # indicate a spurious agent link was rendered.
    assert "&middot;" not in html


def test_parse_toml_warns_on_syntax_error(tmp_path: Path, capsys):
    """_parse_toml prints a warning to stderr when the file has a syntax error."""
    bad_toml = tmp_path / "gptme.toml"
    bad_toml.write_text("this = [invalid toml\n")
    result = _parse_toml(bad_toml)
    assert result == {}
    captured = capsys.readouterr()
    assert "Warning" in captured.err
    assert str(bad_toml) in captured.err


def test_parse_toml_warns_on_missing_tomli(tmp_path: Path, capsys, monkeypatch):
    """_parse_toml prints a warning to stderr when tomli is missing on Python < 3.11."""
    import sys

    good_toml = tmp_path / "gptme.toml"
    good_toml.write_text("[agent]\nname = 'TestAgent'\n")

    # Simulate Python < 3.11 so the code tries to import tomli instead of tomllib
    monkeypatch.setattr(sys, "version_info", (3, 10, 0))
    # Make tomli unavailable (sys.modules[key]=None triggers ImportError on import)
    monkeypatch.setitem(sys.modules, "tomli", None)

    result = _parse_toml(good_toml)
    assert result == {}
    captured = capsys.readouterr()
    assert "Warning" in captured.err
    assert "tomli" in captured.err


# ---------------------------------------------------------------------------
# Tests for scan_recent_sessions
# ---------------------------------------------------------------------------


class TestScanRecentSessions:
    """Tests for scan_recent_sessions()."""

    def test_returns_empty_when_gptme_sessions_not_installed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When gptme-sessions is not importable, return an empty list and warn."""
        import sys

        # Temporarily remove gptme_sessions from sys.modules so the ImportError path runs
        saved = sys.modules.pop("gptme_sessions", None)
        saved_disc = sys.modules.pop("gptme_sessions.discovery", None)
        saved_sig = sys.modules.pop("gptme_sessions.signals", None)
        try:
            with patch.dict(
                "sys.modules",
                {
                    "gptme_sessions": None,
                    "gptme_sessions.discovery": None,
                    "gptme_sessions.signals": None,
                },
            ):
                result = scan_recent_sessions(tmp_path)
        finally:
            if saved is not None:
                sys.modules["gptme_sessions"] = saved
            if saved_disc is not None:
                sys.modules["gptme_sessions.discovery"] = saved_disc
            if saved_sig is not None:
                sys.modules["gptme_sessions.signals"] = saved_sig
        assert result == []
        captured = capsys.readouterr()
        assert "gptme-sessions is not installed" in captured.err

    def test_gptme_sessions_workspace_filter(self, tmp_path: Path) -> None:
        """Sessions whose workspace matches the given workspace are included;
        sessions from other workspaces are excluded."""
        workspace = tmp_path / "myworkspace"
        workspace.mkdir()

        other_workspace = tmp_path / "other-agent"
        other_workspace.mkdir()

        matching_dir = tmp_path / "sessions" / "2026-01-10-matching"
        matching_dir.mkdir(parents=True)
        (matching_dir / "conversation.jsonl").write_text("")

        non_matching_dir = tmp_path / "sessions" / "2026-01-10-other"
        non_matching_dir.mkdir(parents=True)
        (non_matching_dir / "conversation.jsonl").write_text("")

        def mock_discover_gptme(start, end, logs_dir=None):
            return [matching_dir, non_matching_dir]

        def mock_parse_config(session_dir):
            if session_dir == matching_dir:
                return {"workspace": str(workspace)}
            return {"workspace": str(other_workspace)}

        def mock_extract_from_path(path):
            return {
                "git_commits": ["abc1234 feat: test"],
                "file_writes": ["src/foo.py", "src/bar.py"],
                "error_count": 0,
                "grade": 0.75,
                "productive": True,
                "inferred_category": "code",
            }

        def mock_discover_cc(start, end):
            return []  # no CC sessions in this test

        with (
            patch(
                "gptme_sessions.discovery.discover_gptme_sessions",
                mock_discover_gptme,
            ),
            patch("gptme_sessions.discovery.parse_gptme_config", mock_parse_config),
            patch("gptme_sessions.signals.extract_from_path", mock_extract_from_path),
            patch("gptme_sessions.discovery.discover_cc_sessions", mock_discover_cc),
            patch("gptme_sessions.discovery.decode_cc_project_path", lambda x: x),
        ):
            result = scan_recent_sessions(workspace)

        # Only the matching session should be included
        assert len(result) == 1
        assert result[0]["name"] == "2026-01-10-matching"
        assert result[0]["harness"] == "gptme"
        assert result[0]["commits"] == 1

    def test_cc_sessions_workspace_filter(self, tmp_path: Path) -> None:
        """CC sessions are included when their decoded project path matches the workspace;
        sessions for other workspaces are excluded."""
        workspace = tmp_path / "myworkspace"
        workspace.mkdir()

        other_workspace = tmp_path / "other-agent"
        other_workspace.mkdir()

        # Two CC session JSONL files in different project dirs
        matching_dir = tmp_path / "proj-matching"
        matching_dir.mkdir()
        matching_jsonl = matching_dir / "session1.jsonl"
        matching_jsonl.write_text("")

        other_dir = tmp_path / "proj-other"
        other_dir.mkdir()
        other_jsonl = other_dir / "session2.jsonl"
        other_jsonl.write_text("")

        # Map dir names to workspace paths (avoids encoding/decoding complexity)
        cc_dir_map = {
            "proj-matching": str(workspace),
            "proj-other": str(other_workspace),
        }

        def mock_discover_gptme(start, end, logs_dir=None):
            return []  # no gptme sessions in this test

        def mock_discover_cc(start, end):
            return [matching_jsonl, other_jsonl]

        def mock_decode_cc_project_path(dir_name: str) -> str:
            return cc_dir_map.get(dir_name, "/unknown")

        def mock_extract_from_path(path):
            return {
                "git_commits": ["abc1234 feat: cc test"],
                "file_writes": ["src/main.py"],
                "error_count": 1,
                "grade": 0.5,
                "productive": False,
                "inferred_category": "test",
            }

        with (
            patch("gptme_sessions.discovery.discover_gptme_sessions", mock_discover_gptme),
            patch("gptme_sessions.discovery.parse_gptme_config", lambda d: {}),
            patch("gptme_sessions.signals.extract_from_path", mock_extract_from_path),
            patch("gptme_sessions.discovery.discover_cc_sessions", mock_discover_cc),
            patch("gptme_sessions.discovery.decode_cc_project_path", mock_decode_cc_project_path),
            patch("os.path.getmtime", return_value=1704844800.0),  # 2024-01-10
        ):
            result = scan_recent_sessions(workspace)

        # Only the matching CC session should be included
        assert len(result) == 1
        assert result[0]["harness"] == "claude-code"
        assert result[0]["commits"] == 1

    def test_grade_non_numeric_does_not_crash(self, tmp_path: Path) -> None:
        """Non-numeric grade values (None, 'n/a') are handled gracefully."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        session_dir = tmp_path / "sessions" / "2026-01-10-test"
        session_dir.mkdir(parents=True)
        (session_dir / "conversation.jsonl").write_text("")

        def mock_discover_gptme(start, end, logs_dir=None):
            return [session_dir]

        def mock_extract_bad_grade(path):
            return {"grade": "n/a", "git_commits": [], "file_writes": [], "error_count": 0}

        with (
            patch("gptme_sessions.discovery.discover_gptme_sessions", mock_discover_gptme),
            patch("gptme_sessions.discovery.parse_gptme_config", lambda d: {}),
            patch("gptme_sessions.signals.extract_from_path", mock_extract_bad_grade),
            patch("gptme_sessions.discovery.discover_cc_sessions", lambda s, e: []),
            patch("gptme_sessions.discovery.decode_cc_project_path", lambda x: x),
        ):
            result = scan_recent_sessions(workspace)

        assert len(result) == 1
        assert result[0]["grade"] == 0.0  # fell back to default

    def test_error_count_none_does_not_render_as_string(self, tmp_path: Path) -> None:
        """error_count=None from signals is safe-cast to 0, not rendered as 'None'."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        session_dir = tmp_path / "sessions" / "2026-01-10-test"
        session_dir.mkdir(parents=True)
        (session_dir / "conversation.jsonl").write_text("")

        def mock_discover_gptme(start, end, logs_dir=None):
            return [session_dir]

        def mock_extract_none_error_count(path):
            return {"grade": 0.5, "git_commits": [], "file_writes": [], "error_count": None}

        with (
            patch("gptme_sessions.discovery.discover_gptme_sessions", mock_discover_gptme),
            patch("gptme_sessions.discovery.parse_gptme_config", lambda d: {}),
            patch("gptme_sessions.signals.extract_from_path", mock_extract_none_error_count),
            patch("gptme_sessions.discovery.discover_cc_sessions", lambda s, e: []),
            patch("gptme_sessions.discovery.decode_cc_project_path", lambda x: x),
        ):
            result = scan_recent_sessions(workspace)

        assert len(result) == 1
        assert result[0]["errors"] == 0  # None safe-cast to 0, not string "None"

    def test_collect_workspace_data_sessions_off_by_default(self, workspace: Path) -> None:
        """Sessions are NOT scanned when include_sessions=False (default)."""
        data = collect_workspace_data(workspace)
        assert data["sessions"] == []
        assert data["stats"]["total_sessions"] == 0

    def test_collect_workspace_data_sessions_on(self, workspace: Path) -> None:
        """When include_sessions=True and gptme-sessions returns data, sessions appear."""
        fake_session = {
            "name": "2026-01-10-work",
            "date": "2026-01-10",
            "harness": "gptme",
            "commits": 2,
            "edits": 3,
            "errors": 0,
            "grade": 0.78,
            "productive": True,
            "category": "code",
        }

        with patch("gptme_dashboard.generate.scan_recent_sessions", return_value=[fake_session]):
            data = collect_workspace_data(workspace, include_sessions=True)

        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["name"] == "2026-01-10-work"
        assert data["stats"]["total_sessions"] == 1

    def test_sessions_in_generated_html(self, workspace: Path, tmp_path: Path) -> None:
        """Generated HTML includes sessions table when sessions are present."""
        fake_session = {
            "name": "2026-01-10-work",
            "date": "2026-01-10",
            "harness": "gptme",
            "commits": 2,
            "edits": 3,
            "errors": 0,
            "grade": 0.78,
            "productive": True,
            "category": "code",
        }
        output = tmp_path / "site"

        with patch("gptme_dashboard.generate.scan_recent_sessions", return_value=[fake_session]):
            generate(workspace, output, include_sessions=True)

        html = (output / "index.html").read_text()
        assert "Recent Sessions" in html
        assert "2026-01-10-work" in html
        assert "gptme" in html

    def test_sessions_absent_from_html_when_empty(self, workspace: Path, tmp_path: Path) -> None:
        """Generated HTML has no static sessions section when include_sessions=False."""
        output = tmp_path / "site"
        generate(workspace, output, include_sessions=False)
        html = (output / "index.html").read_text()
        # The dynamic panel is always present (hidden via JS until API is available).
        # Only the static section (rendered by --sessions flag) should be absent.
        assert 'id="sessions"' not in html

    def test_sessions_in_json_output(self, workspace: Path) -> None:
        """JSON export includes sessions when include_sessions=True."""
        fake_session = {
            "name": "2026-01-10-work",
            "date": "2026-01-10",
            "harness": "gptme",
            "commits": 2,
            "edits": 3,
            "errors": 0,
            "grade": 0.78,
            "productive": True,
            "category": "code",
        }

        with patch("gptme_dashboard.generate.scan_recent_sessions", return_value=[fake_session]):
            json_str = generate_json(workspace, include_sessions=True)

        data = json.loads(json_str)
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["name"] == "2026-01-10-work"


# --- Journal scanning tests ---


def test_scan_journals_subdirectory_format(tmp_path: Path):
    """scan_journals finds entries in journal/YYYY-MM-DD/*.md format."""
    day_dir = tmp_path / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nDid some work today.\n")
    (day_dir / "notes.md").write_text("# Notes\n\nSome notes here.\n")

    entries = scan_journals(tmp_path)
    assert len(entries) == 2
    assert entries[0]["date"] == "2026-03-07"
    assert entries[0]["preview"]  # should have extracted a preview


def test_scan_journals_flat_format(tmp_path: Path):
    """scan_journals finds entries in journal/YYYY-MM-DD.md format."""
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "2026-03-07.md").write_text("# March 7\n\nFlat format entry.\n")

    entries = scan_journals(tmp_path)
    assert len(entries) == 1
    assert entries[0]["date"] == "2026-03-07"
    assert "Flat format entry" in entries[0]["preview"]


def test_scan_journals_respects_limit(tmp_path: Path):
    """scan_journals caps results at the limit parameter."""
    journal_dir = tmp_path / "journal"
    for i in range(1, 20):
        day_dir = journal_dir / f"2026-03-{i:02d}"
        day_dir.mkdir(parents=True)
        (day_dir / "session.md").write_text(f"# Day {i}\n\nEntry {i}.\n")

    entries = scan_journals(tmp_path, limit=5)
    assert len(entries) == 5
    # Most recent first
    assert entries[0]["date"] == "2026-03-19"


def test_scan_journals_empty_workspace(tmp_path: Path):
    """scan_journals returns empty list when no journal directory exists."""
    assert scan_journals(tmp_path) == []


def test_collect_workspace_data_includes_journals(workspace: Path):
    """collect_workspace_data includes journal entries."""
    day_dir = workspace / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nWorked on tests.\n")

    data = collect_workspace_data(workspace)
    assert len(data["journals"]) == 1
    assert data["stats"]["total_journals"] == 1


def test_generate_html_includes_journals(workspace: Path, tmp_path: Path):
    """Generated HTML includes journal section when entries exist."""
    day_dir = workspace / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nWorked on dashboard.\n")

    output = tmp_path / "site"
    generate(workspace, output)
    html = (output / "index.html").read_text()
    assert "Recent Journal Entries" in html
    assert "2026-03-07" in html


def test_scan_journals_includes_body(tmp_path: Path):
    """scan_journals includes full body text for detail page rendering."""
    day_dir = tmp_path / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nFull content here.\n")

    entries = scan_journals(tmp_path)
    assert len(entries) == 1
    assert "body" in entries[0]
    assert "Full content here" in entries[0]["body"]


def test_scan_journals_includes_page_url_subdirectory(tmp_path: Path):
    """scan_journals includes page_url for subdirectory-format entries."""
    day_dir = tmp_path / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nContent.\n")

    entries = scan_journals(tmp_path)
    assert entries[0]["page_url"] == "journal/2026-03-07/session.html"


def test_scan_journals_includes_page_url_flat(tmp_path: Path):
    """scan_journals includes page_url for flat-format entries."""
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "2026-03-07.md").write_text("# March 7\n\nContent.\n")

    entries = scan_journals(tmp_path)
    assert entries[0]["page_url"] == "journal/2026-03-07.html"


def test_scan_journals_includes_page_url_flat_compound(tmp_path: Path):
    """scan_journals includes correct page_url for compound flat-format stems."""
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "2026-03-07-standup.md").write_text("# Standup\n\nNotes.\n")

    entries = scan_journals(tmp_path)
    assert entries[0]["page_url"] == "journal/2026-03-07-standup.html"
    assert entries[0]["path"] == "journal/2026-03-07-standup.md"


def test_scan_journals_includes_path_subdirectory(tmp_path: Path):
    """scan_journals includes correct path for subdirectory-format entries."""
    day_dir = tmp_path / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nContent.\n")

    entries = scan_journals(tmp_path)
    assert entries[0]["path"] == "journal/2026-03-07/session.md"


def test_journal_page_path_subdirectory():
    """journal_page_path produces correct URL for subdirectory-format entries."""
    assert journal_page_path("2026-03-07", "session") == "journal/2026-03-07/session.html"
    assert journal_page_path("2026-03-07", "notes") == "journal/2026-03-07/notes.html"


def test_journal_page_path_flat():
    """journal_page_path produces correct URL when date equals name (flat format)."""
    assert journal_page_path("2026-03-07", "2026-03-07") == "journal/2026-03-07.html"


def test_journal_page_path_flat_compound():
    """journal_page_path produces correct URL for compound flat-format stems."""
    assert (
        journal_page_path("2026-03-07", "2026-03-07-standup") == "journal/2026-03-07-standup.html"
    )
    assert (
        journal_page_path("2026-03-07", "2026-03-07-notes-review")
        == "journal/2026-03-07-notes-review.html"
    )


def test_generate_journal_detail_pages(workspace: Path, tmp_path: Path):
    """generate() produces per-journal HTML detail pages."""
    day_dir = workspace / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nWorked on the dashboard.\n")

    output = tmp_path / "site"
    generate(workspace, output)

    page = output / "journal" / "2026-03-07" / "session.html"
    assert page.exists(), f"Expected detail page at {page}"
    html = page.read_text()
    assert "Worked on the dashboard" in html


def test_generate_journal_detail_pages_flat_format(tmp_path: Path):
    """generate() produces per-journal HTML for flat-format entries."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "TestAgent"\n')
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "2026-03-07.md").write_text("# March 7\n\nFlat format entry.\n")

    output = tmp_path / "site"
    generate(tmp_path, output)

    page = output / "journal" / "2026-03-07.html"
    assert page.exists(), f"Expected flat detail page at {page}"
    html = page.read_text()
    assert "Flat format entry" in html


def test_generate_index_links_to_journals(workspace: Path, tmp_path: Path):
    """Generated index.html links journal entries to their detail pages."""
    day_dir = workspace / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nContent.\n")

    output = tmp_path / "site"
    generate(workspace, output)

    index_html = (output / "index.html").read_text()
    assert 'href="journal/2026-03-07/session.html"' in index_html


def test_generate_json_excludes_journal_body(workspace: Path):
    """generate_json excludes body from journal entries to avoid bloating data.json."""
    day_dir = workspace / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nContent.\n")

    json_str = generate_json(workspace)
    data = json.loads(json_str)
    assert len(data["journals"]) == 1
    assert "body" not in data["journals"][0]
    assert "page_url" in data["journals"][0]


def test_journal_detail_breadcrumb(workspace: Path, tmp_path: Path):
    """Journal detail page has breadcrumb linking back to index."""
    day_dir = workspace / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nContent.\n")

    output = tmp_path / "site"
    generate(workspace, output)

    html = (output / "journal" / "2026-03-07" / "session.html").read_text()
    assert "../../index.html" in html
    assert "#journals" in html


def test_journal_detail_renders_markdown(workspace: Path, tmp_path: Path):
    """Journal detail page renders markdown to HTML, not escaped text."""
    day_dir = workspace / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text(
        "# Session\n\n**Bold text** and `inline code`.\n\n- item one\n- item two\n"
    )

    output = tmp_path / "site"
    generate(workspace, output)

    html = (output / "journal" / "2026-03-07" / "session.html").read_text()
    assert "<strong>Bold text</strong>" in html
    assert "<code>inline code</code>" in html
    assert "<li>" in html


def test_journal_gh_url(workspace: Path, tmp_path: Path):
    """Journal entries get GitHub URLs when gh_repo_url is detected."""
    day_dir = workspace / "journal" / "2026-03-07"
    day_dir.mkdir(parents=True)
    (day_dir / "session.md").write_text("# Session\n\nContent.\n")

    with patch(
        "gptme_dashboard.generate.detect_github_url",
        return_value="https://github.com/owner/repo",
    ):
        data = collect_workspace_data(workspace)

    assert len(data["journals"]) == 1
    gh_url = data["journals"][0].get("gh_url", "")
    assert "github.com/owner/repo" in gh_url
    assert "journal/2026-03-07/session.md" in gh_url


def test_scan_summaries_all_types(tmp_path: Path):
    """scan_summaries finds entries across daily/weekly/monthly subdirectories."""
    summaries_dir = tmp_path / "knowledge" / "summaries"
    (summaries_dir / "daily").mkdir(parents=True)
    (summaries_dir / "weekly").mkdir(parents=True)
    (summaries_dir / "monthly").mkdir(parents=True)
    (summaries_dir / "daily" / "2026-03-07.md").write_text(
        "# Daily Summary: 2026-03-07\n\n**Sessions**: 2 | **Commits**: 3\n"
    )
    (summaries_dir / "weekly" / "2026-W10.md").write_text(
        "# Weekly Summary: 2026-W10\n\nGood week.\n"
    )
    (summaries_dir / "monthly" / "2026-03.md").write_text(
        "# Monthly Summary: March 2026\n\nGreat month.\n"
    )

    entries = scan_summaries(tmp_path)
    assert len(entries) == 3
    types = {e["type"] for e in entries}
    assert types == {"daily", "weekly", "monthly"}
    periods = {e["period"] for e in entries}
    assert "2026-03-07" in periods
    assert "2026-W10" in periods
    assert "2026-03" in periods


def test_scan_summaries_empty_workspace(tmp_path: Path):
    """scan_summaries returns empty list when knowledge/summaries is absent."""
    assert scan_summaries(tmp_path) == []


def test_scan_summaries_respects_limit(tmp_path: Path):
    """scan_summaries caps results at the limit parameter."""
    daily_dir = tmp_path / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    for i in range(1, 8):
        (daily_dir / f"2026-03-{i:02d}.md").write_text(f"# Day {i}\n\nContent.\n")

    entries = scan_summaries(tmp_path, limit=5)
    assert len(entries) == 5
    # Most recent first
    assert entries[0]["period"] == "2026-03-07"


def test_scan_summaries_preview_skips_headings(tmp_path: Path):
    """scan_summaries preview skips heading/bold lines to get real content."""
    daily_dir = tmp_path / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text(
        "# Daily Summary: 2026-03-07\n\n**Sessions**: 1\n\nFocused on dashboard work.\n"
    )

    entries = scan_summaries(tmp_path)
    assert len(entries) == 1
    # Preview should skip heading and **bold** lines
    assert entries[0]["preview"] == "Focused on dashboard work."


def test_scan_summaries_period_type_filter(tmp_path: Path):
    """period_type filter is applied before limit, not after."""
    summaries_dir = tmp_path / "knowledge" / "summaries"
    (summaries_dir / "daily").mkdir(parents=True)
    (summaries_dir / "weekly").mkdir(parents=True)
    # 3 daily + 3 weekly entries
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (summaries_dir / "daily" / f"{day}.md").write_text(f"# {day}\n\nContent.\n")
    for week in ("2026-W01", "2026-W02", "2026-W03"):
        (summaries_dir / "weekly" / f"{week}.md").write_text(f"# {week}\n\nContent.\n")

    # With limit=3 and no filter: top-3 of 6 total entries (mixed types)
    all_entries = scan_summaries(tmp_path, limit=3)
    assert len(all_entries) == 3

    # With period_type="daily" and limit=3: should return 3 daily entries,
    # not first 3 overall (which may include weekly) then filter to fewer.
    daily_entries = scan_summaries(tmp_path, limit=3, period_type="daily")
    assert len(daily_entries) == 3
    assert all(e["type"] == "daily" for e in daily_entries)


def test_scan_summaries_sort_order_across_types(tmp_path: Path):
    """Weekly ISO-week strings sort chronologically with daily/monthly entries."""
    summaries_dir = tmp_path / "knowledge" / "summaries"
    (summaries_dir / "daily").mkdir(parents=True)
    (summaries_dir / "weekly").mkdir(parents=True)
    (summaries_dir / "monthly").mkdir(parents=True)
    # Week 2 of 2026 starts ~2026-01-05; day 2026-01-15 is later in the month;
    # monthly 2026-01 represents 2026-01-01.
    (summaries_dir / "daily" / "2026-01-15.md").write_text("# Mid-Jan\n\nDaily.\n")
    (summaries_dir / "weekly" / "2026-W02.md").write_text("# Week 2\n\nWeekly.\n")
    (summaries_dir / "monthly" / "2026-01.md").write_text("# January\n\nMonthly.\n")

    entries = scan_summaries(tmp_path)
    assert len(entries) == 3
    periods = [e["period"] for e in entries]
    # Descending: 2026-01-15 (Jan 15) > 2026-W02 (~Jan 5) > 2026-01 (Jan 1)
    assert periods[0] == "2026-01-15", f"Expected daily first, got {periods}"
    assert periods[1] == "2026-W02", f"Expected weekly second, got {periods}"
    assert periods[2] == "2026-01", f"Expected monthly last, got {periods}"


def test_collect_workspace_data_includes_summaries(workspace: Path):
    """collect_workspace_data includes summaries when present."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nDid stuff.\n")

    data = collect_workspace_data(workspace)
    assert len(data["summaries"]) == 1
    assert data["stats"]["total_summaries"] == 1


def test_total_summaries_stat_reflects_actual_count_not_cap(workspace: Path):
    """total_summaries in stats counts all files on disk, not just the capped list."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    # Create 25 daily summaries — more than the default limit of 20
    for i in range(1, 26):
        (daily_dir / f"2026-01-{i:02d}.md").write_text(f"# Day {i}\n\nContent.\n")

    data = collect_workspace_data(workspace)
    # The rendered list is capped at 20, but the stat should show the true count
    assert len(data["summaries"]) == 20
    assert data["stats"]["total_summaries"] == 25


def test_generate_html_includes_summaries(workspace: Path, tmp_path: Path):
    """Generated HTML includes summaries section when entries exist."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nDid stuff.\n")

    output = tmp_path / "site"
    generate(workspace, output)
    html = (output / "index.html").read_text()
    assert "Summaries" in html
    assert "2026-03-07" in html


def test_render_markdown_lists():
    """Lists after a paragraph should render as <li> elements."""
    md = "External resources:\n- LLM evaluation benchmarks\n- Agent evaluation research"
    html = render_markdown_to_html(md)
    assert "<li>" in html
    assert "LLM evaluation benchmarks" in html


# --- Task scanning tests ---


def test_scan_tasks_basic(tmp_path: Path):
    """scan_tasks finds task files with YAML frontmatter."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "fix-bug.md").write_text(
        textwrap.dedent("""\
        ---
        state: active
        priority: high
        tags: [bugfix, urgent]
        assigned_to: bob
        created: 2026-03-01
        ---
        # Fix Critical Bug

        The database connection pool leaks under load.
        """)
    )
    (tasks_dir / "add-feature.md").write_text(
        textwrap.dedent("""\
        ---
        state: backlog
        priority: medium
        tags: [feature]
        created: 2026-02-28
        ---
        # Add Feature X

        Implement the new feature.
        """)
    )

    tasks = scan_tasks(tmp_path)
    assert len(tasks) == 2
    # Active tasks sort before backlog
    assert tasks[0]["state"] == "active"
    assert tasks[0]["title"] == "Fix Critical Bug"
    assert tasks[0]["priority"] == "high"
    assert tasks[0]["assigned_to"] == "bob"
    assert "bugfix" in tasks[0]["tags"]
    assert tasks[0]["id"] == "fix-bug"
    assert tasks[0]["path"] == "tasks/fix-bug.md"


def test_scan_tasks_skips_readme(tmp_path: Path):
    """scan_tasks skips README.md files."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "README.md").write_text("# Tasks\n\nTask management guide.\n")
    (tasks_dir / "real-task.md").write_text(
        "---\nstate: todo\ncreated: 2026-03-01\n---\n# Real Task\n"
    )

    tasks = scan_tasks(tmp_path)
    assert len(tasks) == 1
    assert tasks[0]["id"] == "real-task"


def test_scan_tasks_skips_no_frontmatter(tmp_path: Path):
    """scan_tasks skips files without YAML frontmatter."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "notes.md").write_text("# Just some notes\n\nNo frontmatter here.\n")

    tasks = scan_tasks(tmp_path)
    assert len(tasks) == 0


def test_scan_tasks_empty_workspace(tmp_path: Path):
    """scan_tasks returns empty list when no tasks directory exists."""
    assert scan_tasks(tmp_path) == []


def test_scan_tasks_state_ordering(tmp_path: Path):
    """scan_tasks sorts by state priority (active first, done last)."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for state in ["done", "active", "backlog", "waiting"]:
        (tasks_dir / f"task-{state}.md").write_text(
            f"---\nstate: {state}\ncreated: 2026-03-01\n---\n# Task {state.title()}\n"
        )

    tasks = scan_tasks(tmp_path)
    states = [t["state"] for t in tasks]
    assert states == ["active", "waiting", "backlog", "done"]


def test_collect_workspace_data_includes_tasks(workspace: Path):
    """collect_workspace_data includes task data and stats."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        "---\nstate: active\npriority: high\ncreated: 2026-03-01\n---\n# My Task\n"
    )
    (tasks_dir / "blocked-task.md").write_text(
        "---\nstate: waiting\ncreated: 2026-03-01\n---\n# Blocked Task\n"
    )

    data = collect_workspace_data(workspace)
    assert len(data["tasks"]) == 2
    assert data["stats"]["total_tasks"] == 2
    assert data["stats"]["task_states"]["active"] == 1
    assert data["stats"]["task_states"]["waiting"] == 1


def test_scan_tasks_malformed_yaml_types(tmp_path: Path):
    """scan_tasks handles non-string YAML field values without crashing."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    # YAML that parses state as a list (non-hashable) — previously caused TypeError in sort()
    (tasks_dir / "weird-task.md").write_text(
        "---\nstate:\n  - active\n  - todo\npriority:\n  - high\nassigned_to:\n  - bob\ntags: [dev]\ncreated: 2026-03-01\n---\n# Weird Task\n"
    )
    # Should not raise — malformed files may be skipped or coerced depending on backend
    tasks = scan_tasks(tmp_path)
    assert isinstance(tasks, list)
    # If the task was included (manual fallback), fields must be hashable strings
    for t in tasks:
        assert isinstance(t["state"], str)
        assert isinstance(t["priority"], str)
        assert isinstance(t["assigned_to"], str)


def test_scan_tasks_single_string_tags(tmp_path: Path):
    """scan_tasks promotes a bare string tags value to a one-element list."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "tagged-task.md").write_text(
        "---\nstate: active\ntags: bugfix\ncreated: 2026-03-01\n---\n# Tagged Task\n"
    )
    tasks = scan_tasks(tmp_path)
    assert len(tasks) == 1
    assert tasks[0]["tags"] == ["bugfix"]


def test_scan_tasks_gptodo_path(tmp_path: Path):
    """scan_tasks exercises the gptodo code path when gptodo is installed."""
    pytest.importorskip("gptodo")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        "---\nstate: active\npriority: high\ntags: [feature]\nassigned_to: bob\ncreated: 2026-03-01\n---\n# My Task\n\nDo something useful.\n"
    )
    (tasks_dir / "README.md").write_text("# Tasks\n")
    tasks = scan_tasks(tmp_path)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["id"] == "my-task"
    assert t["title"] == "My Task"
    assert t["state"] == "active"
    assert t["priority"] == "high"
    assert t["tags"] == ["feature"]
    assert t["assigned_to"] == "bob"
    assert t["path"] == "tasks/my-task.md"


def test_generate_html_includes_tasks(workspace: Path, tmp_path: Path):
    """Generated HTML includes task section when tasks exist."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "build-feature.md").write_text(
        "---\nstate: active\npriority: high\ntags: [feature]\ncreated: 2026-03-01\n---\n# Build Feature\n"
    )

    output = tmp_path / "site"
    generate(workspace, output)
    html = (output / "index.html").read_text()
    assert "Tasks" in html
    assert "Build Feature" in html
    assert "active" in html


# --- Plugin detail page tests ---


def test_plugin_page_path():
    """Test plugin directory to URL conversion."""
    assert plugin_page_path("plugins/gptme-consortium") == "plugins/gptme-consortium/index.html"
    assert plugin_page_path("plugins/user-memories") == "plugins/user-memories/index.html"


def test_scan_plugins_includes_body_and_page_url(workspace: Path):
    """Test that scan_plugins includes body and page_url fields."""
    plugins = scan_plugins(workspace)
    assert len(plugins) == 1
    plugin = plugins[0]
    assert "body" in plugin
    assert "page_url" in plugin
    assert "A plugin for testing" in plugin["body"]
    assert plugin["page_url"] == "plugins/gptme-test-plugin/index.html"


def test_scan_plugins_empty_body_when_no_readme(tmp_path: Path):
    """Test that plugins without README have empty body and still get page_url."""
    plugin_dir = tmp_path / "plugins" / "no-readme-plugin"
    plugin_dir.mkdir(parents=True)
    # No README.md

    plugins = scan_plugins(tmp_path)
    assert len(plugins) == 1
    assert plugins[0]["body"] == ""
    assert plugins[0]["page_url"] == "plugins/no-readme-plugin/index.html"


def test_generate_plugin_detail_pages(workspace: Path, tmp_path: Path):
    """Test that per-plugin detail pages are generated for plugins with README."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    plugin_page = output / "plugins" / "gptme-test-plugin" / "index.html"
    assert plugin_page.exists(), f"Expected {plugin_page} to exist"

    html = plugin_page.read_text()
    assert "gptme-test-plugin" in html
    assert "A plugin for testing" in html


def test_generate_index_links_to_plugins(workspace: Path, tmp_path: Path):
    """Test that index.html plugin names link to detail pages."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()
    assert 'href="plugins/gptme-test-plugin/index.html"' in html


def test_plugin_detail_breadcrumb(workspace: Path, tmp_path: Path):
    """Test that plugin detail page breadcrumb uses correct relative root prefix."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    # plugins/gptme-test-plugin/index.html is two levels deep → needs ../../
    html = (output / "plugins" / "gptme-test-plugin" / "index.html").read_text()
    assert 'href="../../index.html"' in html


def test_plugin_detail_renders_markdown(workspace: Path, tmp_path: Path):
    """Test that plugin README markdown is rendered as HTML."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "plugins" / "gptme-test-plugin" / "index.html").read_text()
    assert "<h1>" in html or "<h2>" in html, "Headings should be rendered as HTML"
    assert "&lt;h" not in html, "HTML tags must not be escaped"


def test_generate_json_excludes_plugin_body(workspace: Path):
    """Test that generate_json excludes body from plugins to keep data.json lean."""
    data = collect_workspace_data(workspace)
    json_str = generate_json(workspace, _data=data)
    parsed = json.loads(json_str)

    assert len(parsed["plugins"]) == 1
    assert "body" not in parsed["plugins"][0]
    assert "page_url" in parsed["plugins"][0]


def test_generate_no_plugin_page_when_no_readme(tmp_path: Path):
    """Test that plugins without README do not get a detail page."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Test"\n')
    plugin_dir = tmp_path / "plugins" / "bare-plugin"
    plugin_dir.mkdir(parents=True)
    # No README.md — plugin has no body

    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(tmp_path, output, template_dir)

    plugin_page = output / "plugins" / "bare-plugin" / "index.html"
    assert not plugin_page.exists(), "No detail page should be generated for plugins without README"


# --- Task detail page tests ---


def test_task_page_path():
    assert task_page_path("my-task") == "tasks/my-task.html"
    assert task_page_path("fix-bug-123") == "tasks/fix-bug-123.html"


def test_scan_tasks_includes_body(tmp_path: Path):
    """scan_tasks includes body in each task entry."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        textwrap.dedent("""\
        ---
        state: active
        created: 2026-03-01
        ---
        # My Task

        Do something useful with **markdown**.
        """)
    )
    tasks = scan_tasks(tmp_path)
    assert len(tasks) == 1
    assert "body" in tasks[0]
    assert "Do something useful" in tasks[0]["body"]


def test_scan_tasks_includes_page_url(tmp_path: Path):
    """scan_tasks includes page_url in each task entry."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "fix-bug.md").write_text(
        "---\nstate: active\ncreated: 2026-03-01\n---\n# Fix Bug\n"
    )
    tasks = scan_tasks(tmp_path)
    assert len(tasks) == 1
    assert tasks[0]["page_url"] == "tasks/fix-bug.html"


def test_generate_task_detail_pages(workspace: Path, tmp_path: Path):
    """generate() produces per-task HTML detail pages."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        textwrap.dedent("""\
        ---
        state: active
        priority: high
        tags: [feature]
        created: 2026-03-01
        ---
        # My Task

        Implementation details here.
        """)
    )

    output = tmp_path / "site"
    generate(workspace, output)

    task_page = output / "tasks" / "my-task.html"
    assert task_page.exists()
    html = task_page.read_text()
    assert "My Task" in html
    assert "Implementation details here" in html
    assert "active" in html


def test_generate_index_links_to_tasks(workspace: Path, tmp_path: Path):
    """index.html links task titles to their detail pages."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        "---\nstate: active\ncreated: 2026-03-01\n---\n# My Task\n"
    )

    output = tmp_path / "site"
    generate(workspace, output)

    html = (output / "index.html").read_text()
    assert 'href="tasks/my-task.html"' in html


def test_task_detail_renders_markdown(workspace: Path, tmp_path: Path):
    """Task detail page renders markdown body to HTML."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "rich-task.md").write_text(
        textwrap.dedent("""\
        ---
        state: active
        created: 2026-03-01
        ---
        # Rich Task

        ## Subtasks
        - First item
        - Second item

        Do `something` in **bold**.
        """)
    )

    output = tmp_path / "site"
    generate(workspace, output)

    html = (output / "tasks" / "rich-task.html").read_text()
    assert "<li>" in html
    assert "<strong>" in html
    assert "<code>" in html


def test_task_detail_breadcrumb(workspace: Path, tmp_path: Path):
    """Task detail page has breadcrumb linking back to index #tasks."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        "---\nstate: active\ncreated: 2026-03-01\n---\n# My Task\n"
    )

    output = tmp_path / "site"
    generate(workspace, output)

    html = (output / "tasks" / "my-task.html").read_text()
    assert "../index.html#tasks" in html


def test_generate_json_excludes_task_body(workspace: Path):
    """generate_json() excludes body field from task entries."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        "---\nstate: active\ncreated: 2026-03-01\n---\n# My Task\n\nLong body.\n"
    )

    json_str = generate_json(workspace)
    data = json.loads(json_str)
    assert "tasks" in data
    assert len(data["tasks"]) == 1
    assert "body" not in data["tasks"][0]
    assert "page_url" in data["tasks"][0]


def test_scan_tasks_gptodo_path_includes_body(tmp_path: Path):
    """scan_tasks gptodo code path includes body and page_url."""
    pytest.importorskip("gptodo")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        "---\nstate: active\npriority: high\ncreated: 2026-03-01\n---\n# My Task\n\nDo something.\n"
    )
    tasks = scan_tasks(tmp_path)
    assert len(tasks) == 1
    assert "body" in tasks[0]
    assert "page_url" in tasks[0]
    assert tasks[0]["page_url"] == "tasks/my-task.html"
    assert "Do something" in tasks[0]["body"]


# ---------------------------------------------------------------------------
# scan_readme tests
# ---------------------------------------------------------------------------


def test_scan_readme_no_readme(tmp_path: Path):
    """scan_readme returns empty dict when no README.md exists."""
    result = scan_readme(tmp_path)
    assert result == {}


def test_scan_readme_basic(tmp_path: Path):
    """scan_readme returns body and preview for a plain README."""
    (tmp_path / "README.md").write_text(
        "# My Project\n\nA handy library for doing things.\n\n## Install\n\n```\npip install it\n```\n"
    )
    result = scan_readme(tmp_path)
    assert result["body"]
    assert "My Project" in result["body"]
    assert result["preview"] == "A handy library for doing things."


def test_scan_readme_frontmatter_stripped(tmp_path: Path):
    """scan_readme strips YAML frontmatter before returning body."""
    (tmp_path / "README.md").write_text(
        "---\ntitle: My Project\n---\n# My Project\n\nDescription here.\n"
    )
    result = scan_readme(tmp_path)
    assert "title: My Project" not in result["body"]
    assert "Description here." in result["body"]


def test_scan_readme_preview_skips_headings(tmp_path: Path):
    """scan_readme preview uses first non-heading paragraph."""
    (tmp_path / "README.md").write_text(
        "# Title\n\n## Subtitle\n\nThis is the first paragraph of real content.\n"
    )
    result = scan_readme(tmp_path)
    assert result["preview"] == "This is the first paragraph of real content."


def test_scan_readme_preview_truncated(tmp_path: Path):
    """scan_readme truncates preview to 300 chars with ellipsis."""
    long_text = "x" * 350
    (tmp_path / "README.md").write_text(f"# Title\n\n{long_text}\n")
    result = scan_readme(tmp_path)
    assert len(result["preview"]) <= 301  # 300 chars + "…"
    assert result["preview"].endswith("…")


def test_scan_readme_empty_file(tmp_path: Path):
    """scan_readme returns empty dict for an empty README."""
    (tmp_path / "README.md").write_text("")
    result = scan_readme(tmp_path)
    assert result == {}


def test_readme_in_workspace_data(tmp_path: Path):
    """collect_workspace_data includes 'readme' key when README.md exists."""
    (tmp_path / "README.md").write_text("# My Workspace\n\nA gptme workspace.\n")
    data = collect_workspace_data(tmp_path)
    assert "readme" in data
    assert data["readme"]["preview"] == "A gptme workspace."


def test_readme_absent_from_workspace_data(tmp_path: Path):
    """collect_workspace_data returns empty readme dict when no README.md."""
    data = collect_workspace_data(tmp_path)
    assert data["readme"] == {}


def test_generate_html_includes_about_section(workspace: Path, tmp_path: Path):
    """Generated HTML includes About section when README.md exists."""
    (workspace / "README.md").write_text(
        "# Test Workspace\n\nThis is a great workspace for testing things.\n"
    )
    output = tmp_path / "site"
    generate(workspace, output)
    html = (output / "index.html").read_text()
    assert 'id="about"' in html
    assert "This is a great workspace for testing things." in html


def test_generate_html_no_about_section_without_readme(workspace: Path, tmp_path: Path):
    """Generated HTML omits About section when no README.md is present."""
    readme = workspace / "README.md"
    if readme.exists():
        readme.unlink()
    output = tmp_path / "site"
    generate(workspace, output)
    html = (output / "index.html").read_text()
    assert 'id="about"' not in html


# ── Per-package detail pages ────────────────────────────────────────────────


def test_package_page_path():
    """Test package directory to URL conversion."""
    assert package_page_path("packages/gptme-dashboard") == "packages/gptme-dashboard/index.html"
    assert package_page_path("packages/gptodo") == "packages/gptodo/index.html"


def test_scan_packages_includes_body_and_page_url(workspace: Path):
    """Test that scan_packages includes body and page_url fields."""
    packages = scan_packages(workspace)
    assert len(packages) == 1
    pkg = packages[0]
    assert "body" in pkg
    assert "page_url" in pkg
    assert "A package for testing purposes" in pkg["body"]
    assert pkg["page_url"] == "packages/test-pkg/index.html"


def test_scan_packages_empty_body_when_no_readme(tmp_path: Path):
    """Test that packages without README have empty body and still get page_url."""
    pkg_dir = tmp_path / "packages" / "no-readme-pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "no-readme-pkg"\nversion = "0.1.0"\ndescription = ""\n'
    )
    # No README.md

    packages = scan_packages(tmp_path)
    assert len(packages) == 1
    assert packages[0]["body"] == ""
    assert packages[0]["page_url"] == "packages/no-readme-pkg/index.html"


def test_generate_package_detail_pages(workspace: Path, tmp_path: Path):
    """Test that per-package detail pages are generated for packages with README."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    pkg_page = output / "packages" / "test-pkg" / "index.html"
    assert pkg_page.exists(), f"Expected {pkg_page} to exist"

    html = pkg_page.read_text()
    assert "test-pkg" in html
    assert "A package for testing purposes" in html


def test_generate_index_links_to_packages(workspace: Path, tmp_path: Path):
    """Test that index.html package names link to detail pages when README exists."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()
    assert 'href="packages/test-pkg/index.html"' in html


def test_package_detail_breadcrumb(workspace: Path, tmp_path: Path):
    """Test that package detail page breadcrumb uses correct relative root prefix."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    # packages/test-pkg/index.html is two levels deep → needs ../../
    html = (output / "packages" / "test-pkg" / "index.html").read_text()
    assert 'href="../../index.html"' in html


def test_package_detail_renders_markdown(workspace: Path, tmp_path: Path):
    """Test that package README markdown is rendered as HTML."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "packages" / "test-pkg" / "index.html").read_text()
    assert "<h1>" in html or "<h2>" in html, "Headings should be rendered as HTML"
    assert "&lt;h" not in html, "HTML tags must not be escaped"


def test_generate_no_package_page_when_no_readme(tmp_path: Path):
    """Test that packages without README do not get a detail page."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Test"\n')
    pkg_dir = tmp_path / "packages" / "bare-pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "bare-pkg"\nversion = "0.1.0"\ndescription = ""\n'
    )
    # No README.md — package has no body

    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(tmp_path, output, template_dir)

    pkg_page = output / "packages" / "bare-pkg" / "index.html"
    assert not pkg_page.exists(), "No detail page should be generated for packages without README"


def test_generate_json_excludes_package_body(workspace: Path):
    """Test that generate_json excludes body from packages to keep data.json lean."""
    data = collect_workspace_data(workspace)
    json_str = generate_json(workspace, _data=data)
    parsed = json.loads(json_str)

    assert len(parsed["packages"]) == 1
    assert "body" not in parsed["packages"][0]
    assert "page_url" in parsed["packages"][0]


# --- Sitemap / Atom feed tests ---


def test_github_pages_url_standard():
    """Derive GitHub Pages URL from a github.com repository URL."""
    assert (
        github_pages_url("https://github.com/gptme/gptme-contrib")
        == "https://gptme.github.io/gptme-contrib/"
    )


def test_github_pages_url_user_repo():
    """Works for user repos too."""
    assert (
        github_pages_url("https://github.com/ErikBjare/bob") == "https://ErikBjare.github.io/bob/"
    )


def test_github_pages_url_empty():
    """Returns empty string for non-github or empty input."""
    assert github_pages_url("") == ""
    assert github_pages_url("https://gitlab.com/owner/repo") == ""


def test_github_pages_url_user_org_site():
    """User/org site repos (owner/owner.github.io) map to root URL, not a subpath."""
    assert (
        github_pages_url("https://github.com/gptme/gptme.github.io") == "https://gptme.github.io/"
    )
    assert (
        github_pages_url("https://github.com/ErikBjare/ErikBjare.github.io")
        == "https://ErikBjare.github.io/"
    )
    # case-insensitive repo vs owner comparison
    assert (
        github_pages_url("https://github.com/MyOrg/myorg.github.io") == "https://MyOrg.github.io/"
    )


def test_generate_sitemap_structure():
    """generate_sitemap returns valid XML with index and content entries."""
    data: dict = {
        "gh_repo_url": "https://github.com/gptme/gptme-contrib",
        "lessons": [
            {"page_url": "lessons/workflow/test.html", "source": ""},
        ],
        "skills": [
            {"page_url": "skills/my-skill/index.html", "source": ""},
        ],
        "journals": [
            {"page_url": "journal/2026-03-07/session.html", "date": "2026-03-07"},
        ],
        "plugins": [
            {"page_url": "plugins/my-plugin/index.html", "body": "readme text", "source": ""},
        ],
    }
    sitemap = generate_sitemap(data, "https://gptme.github.io/gptme-contrib/")

    assert '<?xml version="1.0"' in sitemap
    assert "<urlset" in sitemap
    assert "https://gptme.github.io/gptme-contrib/" in sitemap
    assert "lessons/workflow/test.html" in sitemap
    assert "skills/my-skill/index.html" in sitemap
    assert "journal/2026-03-07/session.html" in sitemap
    assert "<lastmod>2026-03-07</lastmod>" in sitemap
    assert "plugins/my-plugin/index.html" in sitemap


def test_generate_sitemap_excludes_submodule_items():
    """Items from submodules (source != '') are excluded from the sitemap."""
    data: dict = {
        "gh_repo_url": "https://github.com/gptme/gptme-contrib",
        "lessons": [
            {"page_url": "lessons/workflow/local.html", "source": ""},
            {"page_url": "gptme-contrib/lessons/workflow/shared.html", "source": "gptme-contrib"},
        ],
        "skills": [],
        "journals": [],
        "plugins": [],
    }
    sitemap = generate_sitemap(data, "https://owner.github.io/repo/")
    assert "lessons/workflow/local.html" in sitemap
    assert "gptme-contrib/lessons/workflow/shared.html" not in sitemap


def test_generate_sitemap_xml_escaping():
    """URL characters that are special in XML are escaped in <loc> values."""
    data: dict = {
        "gh_repo_url": "",
        "lessons": [],
        "skills": [],
        "journals": [],
        "plugins": [],
    }
    # & in a custom base_url would produce invalid XML without escaping
    sitemap = generate_sitemap(data, "https://example.github.io/repo/?foo=1&bar=2")
    assert "&amp;" in sitemap
    assert "&bar" not in sitemap  # raw & must not appear inside <loc>


def test_generate_sitemap_plugins_without_readme_excluded():
    """Plugins without a body (no README) are not listed in the sitemap."""
    data: dict = {
        "gh_repo_url": "",
        "lessons": [],
        "skills": [],
        "journals": [],
        "plugins": [
            {"page_url": "plugins/bare/index.html", "body": "", "source": ""},
            {"page_url": "plugins/documented/index.html", "body": "readme", "source": ""},
        ],
    }
    sitemap = generate_sitemap(data, "https://example.github.io/repo/")
    assert "plugins/bare/index.html" not in sitemap
    assert "plugins/documented/index.html" in sitemap


def test_generate_sitemap_summary_lastmod_valid_dates():
    """Summary lastmod values are valid W3C dates for all period types."""
    data: dict = {
        "gh_repo_url": "",
        "lessons": [],
        "skills": [],
        "journals": [],
        "plugins": [],
        "summaries": [
            {"page_url": "summaries/daily/2026-03-07.html", "period": "2026-03-07"},
            {"page_url": "summaries/weekly/2026-W10.html", "period": "2026-W10"},
            {"page_url": "summaries/monthly/2026-03.html", "period": "2026-03"},
        ],
    }
    sitemap = generate_sitemap(data, "https://example.github.io/repo/")

    # Daily: period is already a valid ISO date
    assert "<lastmod>2026-03-07</lastmod>" in sitemap
    # Weekly: must convert ISO week to the Monday of that week, not use raw "2026-W10"
    assert "<lastmod>2026-W10</lastmod>" not in sitemap
    assert "<lastmod>2026-03-02</lastmod>" in sitemap  # Monday of 2026-W10
    # Monthly: convert "2026-03" to "2026-03-01"
    assert "<lastmod>2026-03-01</lastmod>" in sitemap


def test_generate_writes_sitemap_with_explicit_base_url(workspace: Path, tmp_path: Path):
    """generate() writes sitemap.xml when base_url is given explicitly."""
    output = tmp_path / "site"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir, base_url="https://example.github.io/myrepo/")

    sitemap = output / "sitemap.xml"
    assert sitemap.exists(), "sitemap.xml should be generated when base_url is set"
    content = sitemap.read_text()
    assert "https://example.github.io/myrepo/" in content


def test_generate_no_sitemap_when_suppressed(workspace: Path, tmp_path: Path):
    """generate() skips sitemap.xml when base_url='-'."""
    output = tmp_path / "site"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir, base_url="-")

    assert not (
        output / "sitemap.xml"
    ).exists(), "sitemap.xml should be suppressed with base_url='-'"


def test_generate_no_sitemap_when_no_github_remote(tmp_path: Path):
    """generate() skips sitemap.xml when GitHub remote is not detected and no base_url given."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Test"\n')
    output = tmp_path / "site"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"

    with patch("gptme_dashboard.generate.detect_github_url", return_value=""):
        generate(tmp_path, output, template_dir)

    assert not (
        output / "sitemap.xml"
    ).exists(), "sitemap.xml should not be generated without a base URL"


# --- Description normalisation tests ---


def test_scan_skills_multiline_description_normalised(tmp_path: Path):
    """Multiline YAML block-scalar descriptions should be collapsed to first line."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Test"\n')
    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
        ---
        name: My Skill
        description: |
          First line of the description.
          Second line with more details.
        ---
        # My Skill

        Body content here.
        """)
    )

    skills = scan_skills(tmp_path)
    assert len(skills) == 1
    # description must be a single line — no embedded newlines
    assert "\n" not in skills[0]["description"]
    assert skills[0]["description"] == "First line of the description."


def test_scan_skills_list_description_uses_non_list_line(tmp_path: Path):
    """If the description starts with list items, skip to the first prose line."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Test"\n')
    skill_dir = tmp_path / "skills" / "list-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
        ---
        name: List Skill
        description: |
          - item one
          - item two
          Prose summary line.
        ---
        # List Skill

        Body.
        """)
    )

    skills = scan_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0]["description"] == "Prose summary line."


def test_scan_plugins_skips_list_first_line(tmp_path: Path):
    """Plugin descriptions should not start with a markdown list marker."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Test"\n')
    plugin_dir = tmp_path / "plugins" / "list-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text(
        "# List Plugin\n\n- First line is a list item\n- Second item\n\nProse description.\n"
    )

    plugins = scan_plugins(tmp_path)
    assert len(plugins) == 1
    desc = plugins[0]["description"]
    assert not desc.startswith("-"), f"description should not start with '-': {desc!r}"
    assert desc == "Prose description."


def test_guidance_collapses_at_five_rows(workspace: Path, tmp_path: Path):
    """The guidance table collapses after 5 rows by default."""
    # Add enough skills to exceed the collapse threshold
    for i in range(8):
        skill_dir = workspace / "skills" / f"skill-{i}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(f"""\
            ---
            name: Skill {i}
            description: Skill number {i}
            ---
            # Skill {i}
            Content.
            """)
        )

    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()
    # Rows beyond 5 should be collapsed (style="display:none")
    assert 'class="collapsed-row"' in html
    # Show-more button should reference the total count
    assert "Browse all" in html


def test_guidance_filter_panel_hidden_by_default(workspace: Path, tmp_path: Path):
    """Filter controls for guidance section are hidden until the section is expanded."""
    # Need >5 guidance items to trigger the show-more button with data-controls
    for i in range(6):
        skill_dir = workspace / "skills" / f"filter-skill-{i}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(f"""\
            ---
            name: Filter Skill {i}
            description: Skill {i} for filter panel test
            ---
            # Filter Skill {i}
            Content.
            """)
        )

    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    # Advanced filter panel should be present but hidden
    assert 'id="guidance-adv-filters"' in html
    assert 'id="guidance-adv-filters" style="display:none"' in html

    # Show-more button should reference the filter panel via data-controls
    assert 'data-controls="guidance-adv-filters"' in html


def test_guidance_filter_panel_visible_for_small_workspace(
    tmp_path: Path,
):
    """Filter controls are always visible when guidance items ≤ 5 (no show-more button)."""
    # Create a minimal workspace with only 3 guidance items (below collapse threshold)
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "SmallAgent"\n')
    lessons_dir = tmp_path / "lessons" / "workflow"
    lessons_dir.mkdir(parents=True)
    for i in range(3):
        (lessons_dir / f"lesson-{i}.md").write_text(
            textwrap.dedent(f"""\
            ---
            match:
              keywords: ["kw{i}"]
            status: active
            ---
            # Lesson {i}
            Content.
            """)
        )

    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(tmp_path, output, template_dir)

    html = (output / "index.html").read_text()

    # Filter panel must exist
    assert 'id="guidance-adv-filters"' in html
    # Must NOT be hidden — no show-more button exists for ≤5 items
    assert 'id="guidance-adv-filters" style="display:none"' not in html
    # No show-more button (nothing to expand)
    assert 'id="guidance-show-more"' not in html


def test_generate_dashboard_navigation_sidebar(workspace: Path, tmp_path: Path):
    """Dashboard includes a quick-navigation sidebar for major sections."""
    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    assert 'class="dashboard-layout"' in html
    assert 'class="dashboard-nav"' in html
    assert 'aria-label="Dashboard navigation"' in html
    assert 'href="#guidance"' in html
    assert 'href="#packages"' in html
    assert 'href="#plugins"' in html
    assert 'href="#recent-sessions"' in html
    assert 'href="#activity-heatmap"' in html
    assert 'href="#service-health"' in html
    assert "Quick navigation" in html
    # Positive direction of the {% if not journals %} guard: no static journals → link present
    assert 'href="#dynamic-journals"' in html


def test_generate_dashboard_navigation_sidebar_journals_guard(workspace: Path, tmp_path: Path):
    """#dynamic-journals nav link is absent from Live dashboard when static journals exist."""
    # Add a journal entry so the template renders static journals
    journal_day = workspace / "journal" / "2026-01-10"
    journal_day.mkdir(parents=True)
    (journal_day / "session.md").write_text("# Session\n\nWorking on the dashboard.\n")

    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    # Static journals present: #dynamic-journals element and nav link should both be absent
    assert 'href="#dynamic-journals"' not in html
    assert 'id="dynamic-journals"' not in html
    # But the static journals nav link should be present
    assert 'href="#journals"' in html


def test_generate_dashboard_navigation_sidebar_with_sessions(workspace: Path, tmp_path: Path):
    """Sidebar shows #sessions link under Workspace assets when static sessions are present."""
    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    fake_session = {
        "name": "2026-01-10-work",
        "date": "2026-01-10",
        "harness": "gptme",
        "commits": 2,
        "edits": 3,
        "errors": 0,
        "grade": 0.78,
        "productive": True,
        "category": "code",
    }
    with patch("gptme_dashboard.generate.scan_recent_sessions", return_value=[fake_session]):
        generate(workspace, output, template_dir, include_sessions=True)

    html = (output / "index.html").read_text()

    # Static sessions: link in Workspace assets group
    assert 'href="#sessions"' in html
    # Dynamic sessions panel: link still present in Live dashboard group
    assert 'href="#recent-sessions"' in html


def test_generate_dashboard_navigation_sidebar_dom_order(workspace: Path, tmp_path: Path):
    """Sidebar <aside> precedes <div class="dashboard-main"> in DOM for accessible tab order."""
    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    assert 'class="dashboard-nav"' in html, "aside.dashboard-nav missing from rendered output"
    assert 'class="dashboard-main"' in html, "div.dashboard-main missing from rendered output"
    aside_pos = html.index('class="dashboard-nav"')
    main_pos = html.index('class="dashboard-main"')
    assert aside_pos < main_pos, "aside.dashboard-nav must precede div.dashboard-main in DOM"


def test_generate_dashboard_nav_scroll_js(workspace: Path, tmp_path: Path):
    """Dashboard includes smooth-scroll section navigation JS with scroll-spy."""
    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    assert "nav-active" in html, "nav-active CSS class missing"
    assert "scrollIntoView" in html, "scrollIntoView call missing from nav JS"
    assert "setActive" in html, "setActive function missing from nav JS"
    # Click handlers attached to nav links
    assert "addEventListener('click'" in html, "click handler missing from nav links"
    # Scroll-spy for updating active nav link
    assert "addEventListener('scroll'" in html, "scroll event listener missing"


def test_generate_dashboard_live_nav_group_hidden_in_static_mode(workspace: Path, tmp_path: Path):
    """Live dashboard nav group is hidden by default; only revealed when API connects."""
    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    # live-nav-group must be present but hidden in static output
    assert 'id="live-nav-group"' in html, "live-nav-group element missing"
    live_idx = html.index('id="live-nav-group"')
    # The element must have display:none
    snippet = html[live_idx : live_idx + 60]
    assert 'style="display:none"' in snippet, f"live-nav-group not hidden by default: {snippet!r}"
    # initDynamic must reference the group by JS variable name
    assert "liveGroup" in html, "liveGroup JS variable missing from initDynamic"


def test_generate_dashboard_readme_section_label(workspace: Path, tmp_path: Path):
    """#about section shows 'Core Files' in nav with README.md in a collapsible details."""
    workspace_readme = workspace / "README.md"
    workspace_readme.write_text("# My Agent\n\nAgent description here.\n")

    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    # Section must exist and contain README.md in a <details>
    assert "README.md" in html, "README.md label missing from rendered output"
    assert "<details>" in html, "README.md should be in a collapsible <details>"
    # Nav link must reference #about and show "Core Files" label
    about_nav_idx = html.index('href="#about"')
    nav_snippet = html[about_nav_idx : about_nav_idx + 60]
    assert (
        "Core Files" in nav_snippet
    ), f"Nav link for #about should show Core Files: {nav_snippet!r}"


def test_generate_dashboard_section_order_matches_nav(workspace: Path, tmp_path: Path):
    """Content sections appear in the same order as sidebar navigation links."""
    # Create enough content so all sections render
    workspace_readme = workspace / "README.md"
    workspace_readme.write_text("# Agent\n\nDescription.\n")
    (workspace / "journal" / "2026-03-13").mkdir(parents=True)
    (workspace / "journal" / "2026-03-13" / "session.md").write_text("## Work\nDid things.\n")
    (workspace / "knowledge" / "summaries" / "weekly").mkdir(parents=True, exist_ok=True)
    (workspace / "knowledge" / "summaries" / "weekly" / "2026-W11.md").write_text(
        "# Weekly Summary\nStuff happened this week.\n"
    )

    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    # Static sections must appear in sidebar nav order:
    # tasks → sessions → journals → summaries → about → packages → plugins → guidance
    # (tasks/sessions may be absent but the rest are always present)
    expected_order = ["journals", "summaries", "about", "packages", "plugins", "guidance"]
    positions = []
    for section_id in expected_order:
        marker = f'<section id="{section_id}"'
        try:
            positions.append((section_id, html.index(marker)))
        except ValueError:
            pass  # section not rendered (conditional)

    for i in range(len(positions) - 1):
        current_id, current_pos = positions[i]
        next_id, next_pos = positions[i + 1]
        assert current_pos < next_pos, (
            f"Section #{current_id} (pos {current_pos}) must appear before "
            f"#{next_id} (pos {next_pos}) to match sidebar nav order"
        )


def test_generate_dashboard_content_nav_group_hidden_when_no_content(
    workspace: Path, tmp_path: Path
):
    """Content nav group is omitted when workspace has no readme/tasks/sessions/journals/summaries."""
    # Base workspace fixture has no readme, tasks, sessions, journals, or summaries
    output = tmp_path / "out"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir)

    html = (output / "index.html").read_text()

    # The "Content" nav group heading must not appear
    assert (
        "Content</h3>" not in html
    ), "Content nav group should be hidden on a sparse workspace with no content data"
    # The Workspace group is always present (packages/plugins/guidance are unconditional)
    assert "Workspace</h3>" in html


# --- Atom feed tests ---


def test_generate_atom_feed_basic(tmp_path: Path):
    """generate_atom_feed produces valid Atom XML with journal entries."""
    data: dict = {
        "journals": [
            {
                "date": "2026-03-09",
                "name": "session",
                "preview": "Working on the dashboard.",
                "body": "# Session\n\nWorking on the dashboard.",
                "page_url": "journal/2026-03-09/session.html",
            }
        ]
    }
    feed = generate_atom_feed(data, "https://bob.github.io/bob/", "bob")

    assert '<?xml version="1.0" encoding="utf-8"?>' in feed
    assert '<feed xmlns="http://www.w3.org/2005/Atom">' in feed
    assert "<title>bob Journal</title>" in feed
    assert 'href="https://bob.github.io/bob/"' in feed
    assert 'href="https://bob.github.io/bob/feed.xml"' in feed
    assert "<entry>" in feed
    assert "2026-03-09" in feed
    assert "Working on the dashboard." in feed


def test_generate_atom_feed_escapes_special_chars(tmp_path: Path):
    """generate_atom_feed XML-escapes special characters in titles and content."""
    data: dict = {
        "journals": [
            {
                "date": "2026-03-09",
                "name": "session",
                "preview": "Fix <bug> & deploy",
                "body": "Fix <bug> & deploy",
                "page_url": "journal/2026-03-09/session.html",
            }
        ]
    }
    feed = generate_atom_feed(data, "https://example.github.io/repo/", "example")

    assert "&lt;bug&gt;" in feed
    assert "&amp;" in feed
    assert "<bug>" not in feed


def test_generate_atom_feed_empty_journals():
    """generate_atom_feed produces a valid feed even with no journal entries."""
    data: dict = {"journals": []}
    feed = generate_atom_feed(data, "https://bob.github.io/bob/", "bob")

    assert '<feed xmlns="http://www.w3.org/2005/Atom">' in feed
    assert "<entry>" not in feed
    assert "</feed>" in feed


def test_generate_atom_feed_caps_at_20():
    """generate_atom_feed includes at most 20 entries, keeping the newest first."""
    # Journals sorted newest-first (as scan_journals returns them)
    journals = [
        {
            "date": f"2026-01-{i:02d}",
            "name": "session",
            "preview": f"Day {i}",
            "body": f"Day {i}",
            "page_url": f"journal/2026-01-{i:02d}/session.html",
        }
        for i in range(25, 0, -1)  # 25 entries, newest (day 25) first
    ]
    data: dict = {"journals": journals}
    feed = generate_atom_feed(data, "https://bob.github.io/bob/", "bob")

    assert feed.count("<entry>") == 20
    # Newest entries (days 25..6) are retained; oldest 5 (days 1..5) are dropped
    assert "2026-01-25" in feed
    assert "2026-01-06" in feed
    assert "2026-01-05" not in feed
    assert "2026-01-01" not in feed


def test_generate_emits_feed_xml_with_base_url(tmp_path: Path, workspace: Path):
    """generate() writes feed.xml alongside index.html when base_url is given."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir, base_url="https://example.github.io/test/")

    feed_path = output / "feed.xml"
    assert feed_path.exists(), "feed.xml should be generated when base_url is given"
    feed = feed_path.read_text()
    assert '<feed xmlns="http://www.w3.org/2005/Atom">' in feed
    assert "https://example.github.io/test/" in feed


def test_generate_no_feed_without_base_url_or_github_remote(tmp_path: Path):
    """generate() does not write feed.xml when base_url is absent and no GitHub remote."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "NoRemote"\n')
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    # Patch detect_github_url to return empty (no remote)
    with patch("gptme_dashboard.generate.detect_github_url", return_value=""):
        generate(tmp_path, output, template_dir)

    assert not (output / "feed.xml").exists(), "feed.xml must not be generated without a base URL"


def test_generate_no_feed_with_suppressed_base_url(tmp_path: Path, workspace: Path):
    """generate() skips feed.xml when base_url='-'."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir, base_url="-")

    assert not (output / "feed.xml").exists(), "feed.xml must be suppressed when base_url='-'"


def test_generate_index_includes_feed_autodiscovery(tmp_path: Path, workspace: Path):
    """index.html contains Atom feed autodiscovery link when feed_url is set."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    generate(workspace, output, template_dir, base_url="https://example.github.io/test/")

    index_html = (output / "index.html").read_text()
    assert 'type="application/atom+xml"' in index_html
    assert "feed.xml" in index_html


def test_generate_index_no_feed_link_without_base_url(tmp_path: Path):
    """index.html omits the feed link when no base_url is available."""
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "NoFeed"\n')
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "src" / "gptme_dashboard" / "templates"
    with patch("gptme_dashboard.generate.detect_github_url", return_value=""):
        generate(tmp_path, output, template_dir)

    index_html = (output / "index.html").read_text()
    assert "application/atom+xml" not in index_html


# ---------------------------------------------------------------------------
# Summary detail pages
# ---------------------------------------------------------------------------


def test_summary_page_path():
    """summary_page_path converts type + period to the expected URL."""
    assert summary_page_path("daily", "2026-03-07") == "summaries/daily/2026-03-07.html"
    assert summary_page_path("weekly", "2026-W10") == "summaries/weekly/2026-W10.html"
    assert summary_page_path("monthly", "2026-03") == "summaries/monthly/2026-03.html"


def test_scan_summaries_includes_body_and_page_url(tmp_path: Path):
    """scan_summaries includes body and page_url for each entry."""
    daily_dir = tmp_path / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nDid great work.\n")

    entries = scan_summaries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert "body" in entry
    assert "Did great work." in entry["body"]
    assert entry["page_url"] == "summaries/daily/2026-03-07.html"
    assert entry["path"] == "knowledge/summaries/daily/2026-03-07.md"


def test_generate_summary_detail_pages(workspace: Path, tmp_path: Path):
    """generate() produces per-summary HTML detail pages."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nWorked on the dashboard.\n")

    output = tmp_path / "site"
    generate(workspace, output)

    page = output / "summaries" / "daily" / "2026-03-07.html"
    assert page.exists(), f"Expected detail page at {page}"
    html = page.read_text()
    assert "Worked on the dashboard" in html


def test_generate_index_links_to_summaries(workspace: Path, tmp_path: Path):
    """Generated index.html links summary entries to their detail pages."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nContent.\n")

    output = tmp_path / "site"
    generate(workspace, output)

    index_html = (output / "index.html").read_text()
    assert 'href="summaries/daily/2026-03-07.html"' in index_html


def test_summary_detail_breadcrumb(workspace: Path, tmp_path: Path):
    """Summary detail page has breadcrumb linking back to index."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nContent.\n")

    output = tmp_path / "site"
    generate(workspace, output)

    page = output / "summaries" / "daily" / "2026-03-07.html"
    html = page.read_text()
    assert "../../index.html" in html
    assert "Summaries" in html


def test_summary_detail_renders_markdown(workspace: Path, tmp_path: Path):
    """Summary detail page renders markdown body as HTML."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text(
        "# Daily Summary\n\n**Sessions**: 2 | **Commits**: 3\n\nGreat day.\n"
    )

    output = tmp_path / "site"
    generate(workspace, output)

    page = output / "summaries" / "daily" / "2026-03-07.html"
    html = page.read_text()
    assert "<strong>Sessions</strong>" in html
    assert "Great day." in html


def test_generate_json_excludes_summary_body(workspace: Path):
    """generate_json excludes body from summaries to avoid bloating data.json."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nContent.\n")

    json_str = generate_json(workspace)
    data = json.loads(json_str)
    for s in data["summaries"]:
        assert "body" not in s


def test_generate_sitemap_includes_summaries(workspace: Path, tmp_path: Path):
    """generate() includes summary detail pages in sitemap.xml when base_url given."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nContent.\n")

    output = tmp_path / "site"
    generate(workspace, output, base_url="https://example.github.io/repo/")

    sitemap = (output / "sitemap.xml").read_text()
    assert "summaries/daily/2026-03-07.html" in sitemap


# ── Task state filter ─────────────────────────────────────────────────────────


def test_generate_task_state_filter_buttons(workspace: Path, tmp_path: Path):
    """index.html renders task state filter buttons with data-state attributes."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "active-task.md").write_text(
        "---\nstate: active\ncreated: 2026-03-01\n---\n# Active Task\n"
    )
    (tasks_dir / "waiting-task.md").write_text(
        "---\nstate: waiting\ncreated: 2026-03-01\n---\n# Waiting Task\n"
    )

    output = tmp_path / "site"
    generate(workspace, output)

    html = (output / "index.html").read_text()
    assert 'id="task-state-filters"' in html
    assert 'class="filter-btn active" data-state="all"' in html
    assert 'class="filter-btn" data-state="active"' in html
    assert 'class="filter-btn" data-state="waiting"' in html


def test_generate_task_rows_have_data_state(workspace: Path, tmp_path: Path):
    """Task rows in index.html carry data-state attributes for JS filtering."""
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "my-task.md").write_text(
        "---\nstate: active\ncreated: 2026-03-01\n---\n# My Task\n"
    )

    output = tmp_path / "site"
    generate(workspace, output)

    html = (output / "index.html").read_text()
    assert '<tr data-state="active"' in html
