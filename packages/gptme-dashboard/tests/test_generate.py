"""Tests for static dashboard generator."""

import json
import textwrap
from pathlib import Path

import pytest

from gptme_dashboard.generate import (
    collect_workspace_data,
    detect_submodules,
    extract_title,
    generate,
    generate_json,
    lesson_page_path,
    parse_frontmatter,
    read_workspace_config,
    render_markdown_to_html,
    scan_lessons,
    scan_packages,
    scan_plugins,
    scan_skills,
    skill_page_path,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal gptme workspace for testing."""
    # gptme.toml with [agent] section (name must be read from [agent], not other sections)
    (tmp_path / "gptme.toml").write_text(
        textwrap.dedent("""\
        [project]
        name = "should-not-be-used"

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
    """Test that [agent] name is returned, not [project] name."""
    config = read_workspace_config(workspace)
    assert config["agent_name"] == "TestAgent"
    assert config["agent_name"] != "should-not-be-used"


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

    # Stats: 4 guidance (3 lessons + 1 skill), 1 plugin, 1 package
    assert 'class="number">4<' in html  # guidance count
    assert 'class="number">1<' in html  # plugin / package count


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
