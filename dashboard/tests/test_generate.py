"""Tests for static dashboard generator."""

import textwrap
from pathlib import Path

import pytest

from dashboard.generate import (
    extract_title,
    generate,
    parse_frontmatter,
    scan_lessons,
    scan_packages,
    scan_plugins,
    scan_skills,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal gptme workspace for testing."""
    # gptme.toml
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


def test_extract_title():
    """Test title extraction from markdown."""
    assert extract_title("# My Title\nBody", "fallback") == "My Title"
    assert extract_title("No heading here", "fallback") == "fallback"
    assert extract_title("## H2 heading\n# H1 heading", "fb") == "H1 heading"


def test_scan_lessons(workspace: Path):
    """Test lesson scanning."""
    lessons = scan_lessons(workspace)
    assert len(lessons) == 3

    titles = {lesson["title"] for lesson in lessons}
    assert "Test Lesson" in titles
    assert "Shell Safety" in titles
    assert "Old Lesson" in titles

    # Check categories
    categories = {lesson["category"] for lesson in lessons}
    assert "workflow" in categories
    assert "tools" in categories

    # Check keyword parsing
    test_lesson = next(lesson for lesson in lessons if lesson["title"] == "Test Lesson")
    assert "test keyword" in test_lesson["keywords"]
    assert test_lesson["status"] == "active"


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


def test_generate_full(workspace: Path, tmp_path: Path):
    """Test full HTML generation."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "templates"
    generate(workspace, output, template_dir)

    index = output / "index.html"
    assert index.exists()

    html = index.read_text()
    assert "TestAgent" in html  # workspace name from gptme.toml
    assert "Test Lesson" in html
    assert "gptme-test-plugin" in html
    assert "test-pkg" in html
    assert "Test Skill" in html
    assert "0.2.0" in html

    # Check stats rendered
    assert ">3<" in html  # 3 lessons
    assert ">1<" in html  # 1 plugin, 1 package, 1 skill


def test_generate_empty_workspace(tmp_path: Path):
    """Test generation on workspace with no content."""
    output = tmp_path / "output"
    template_dir = Path(__file__).parent.parent / "templates"
    generate(tmp_path, output, template_dir)

    html = (output / "index.html").read_text()
    assert ">0<" in html  # Zero counts
