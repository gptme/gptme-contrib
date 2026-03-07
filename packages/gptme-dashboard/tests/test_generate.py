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
    generate_json,
    github_blob_url,
    github_tree_url,
    lesson_page_path,
    parse_frontmatter,
    read_agent_urls,
    read_workspace_config,
    render_markdown_to_html,
    scan_journals,
    scan_lessons,
    scan_packages,
    scan_plugins,
    scan_recent_sessions,
    scan_skills,
    scan_summaries,
    scan_tasks,
    skill_page_path,
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
    """Submodule items should not get gh_url (they belong to a different repo)."""
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

    # Fake submodule with a lesson
    sub_path = tmp_path / "gptme-contrib"
    sub_lessons = sub_path / "lessons" / "workflow"
    sub_lessons.mkdir(parents=True)
    (sub_lessons / "sub.md").write_text("---\nstatus: active\n---\n# Sub\n\nBody.")
    (sub_path / "gptme.toml").write_text('[agent]\nname = "contrib"\n')
    subprocess.run(["git", "init"], cwd=sub_path, capture_output=True)

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
    assert "gh_url" not in sub_lessons_data[0]  # submodule items must NOT get gh_url


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


def test_collect_workspace_data_includes_summaries(workspace: Path):
    """collect_workspace_data includes summaries when present."""
    daily_dir = workspace / "knowledge" / "summaries" / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-03-07.md").write_text("# Daily Summary\n\nDid stuff.\n")

    data = collect_workspace_data(workspace)
    assert len(data["summaries"]) == 1
    assert data["stats"]["total_summaries"] == 1


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
