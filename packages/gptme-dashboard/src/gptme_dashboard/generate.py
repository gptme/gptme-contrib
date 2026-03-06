"""Static dashboard generator for gptme workspaces.

Scans a gptme workspace (lessons, plugins, packages, skills) and generates
a static HTML site suitable for gh-pages deployment, or a JSON data dump
for custom frontends.

Designed to work with any gptme workspace (gptme-contrib, bob, alice, etc.).
"""

import json
import re
from pathlib import Path

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader


def render_markdown_to_html(md_text: str) -> str:
    """Render markdown text to HTML using the markdown library."""
    return markdown.markdown(
        md_text,
        extensions=["fenced_code", "tables", "codehilite"],
        extension_configs={"codehilite": {"css_class": "code", "noclasses": True}},
    )


def lesson_page_path(lesson_path: str) -> str:
    """Convert a lesson's relative path to its detail page URL path.

    E.g. 'workflow/test-lesson.md' -> 'lessons/workflow/test-lesson.html'
    """
    return "lessons/" + str(Path(lesson_path).with_suffix(".html"))


def skill_page_path(skill_dir: str) -> str:
    """Convert a skill's directory path to its detail page URL path.

    E.g. 'skills/my-skill' -> 'skills/my-skill/index.html'
    """
    return str(Path(skill_dir) / "index.html")


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    """Parse YAML frontmatter from a markdown file."""
    text = path.read_text(errors="replace")
    if not text.startswith("---"):
        return {}, text

    end = text.find("---", 3)
    if end == -1:
        return {}, text

    fm_text = text[3:end].strip()
    body = text[end + 3 :].strip()

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}

    return fm, body


def extract_title(body: str, fallback: str) -> str:
    """Extract first H1 heading from markdown body."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def scan_lessons(workspace: Path) -> list[dict]:
    """Scan lessons directory for lesson files."""
    lessons_dir = workspace / "lessons"
    if not lessons_dir.is_dir():
        return []

    lessons = []
    for md in sorted(lessons_dir.rglob("*.md")):
        if md.name == "README.md":
            continue
        rel = md.relative_to(lessons_dir)
        category = rel.parts[0] if len(rel.parts) > 1 else "uncategorized"

        fm, body = parse_frontmatter(md)
        title = extract_title(body, md.stem.replace("-", " ").title())
        status = fm.get("status", "active")

        keywords: list[str] = []
        match_fm = fm.get("match", {})
        if isinstance(match_fm, dict):
            kw = match_fm.get("keywords", [])
            if isinstance(kw, list):
                keywords = kw
            elif isinstance(kw, str):
                keywords = [kw]

        page_url = lesson_page_path(str(rel))

        lessons.append(
            {
                "title": title,
                "category": category,
                "status": status,
                "keywords": keywords[:5],  # Limit displayed keywords
                "all_keywords": keywords,
                "body": body,
                "path": str(rel),
                "page_url": page_url,
            }
        )

    return lessons


def scan_plugins(workspace: Path) -> list[dict]:
    """Scan plugins directory for plugin directories."""
    plugins_dir = workspace / "plugins"
    if not plugins_dir.is_dir():
        return []

    plugins = []
    for d in sorted(plugins_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue

        readme = d / "README.md"
        description = ""
        if readme.exists():
            _, body = parse_frontmatter(readme)
            for line in body.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:200]
                    break

        plugins.append(
            {
                "name": d.name,
                "description": description,
                "path": str(d.relative_to(workspace)),
            }
        )

    return plugins


def scan_packages(workspace: Path) -> list[dict]:
    """Scan packages directory for Python packages."""
    packages_dir = workspace / "packages"
    if not packages_dir.is_dir():
        return []

    packages = []
    for d in sorted(packages_dir.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue

        pyproject = d / "pyproject.toml"
        description = ""
        version = ""
        if pyproject.exists():
            text = pyproject.read_text()
            in_project_section = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    in_project_section = stripped == "[project]"
                elif in_project_section:
                    if not description:
                        m = re.match(r'description\s*=\s*"([^"]*)"', stripped)
                        if m:
                            description = m.group(1)
                    if not version:
                        m = re.match(r'version\s*=\s*"([^"]*)"', stripped)
                        if m:
                            version = m.group(1)
                    if description and version:
                        break

        packages.append(
            {
                "name": d.name,
                "description": description,
                "version": version,
                "path": str(d.relative_to(workspace)),
            }
        )

    return packages


def scan_skills(workspace: Path) -> list[dict]:
    """Scan skills directory for SKILL.md files."""
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return []

    skills = []
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        fm, body = parse_frontmatter(skill_md)
        name = fm.get("name", skill_md.parent.name.replace("-", " ").title())
        description = fm.get("description", "")

        if not description:
            description = extract_title(body, "")

        rel_dir = str(skill_md.parent.relative_to(workspace))
        page_url = skill_page_path(rel_dir)

        skills.append(
            {
                "name": name,
                "description": description,
                "body": body,
                "path": rel_dir,
                "page_url": page_url,
            }
        )

    return skills


def read_workspace_config(workspace: Path) -> dict:
    """Read gptme.toml for workspace metadata.

    Parses [agent] section specifically to avoid matching name fields
    from other sections like [project].
    """
    config_path = workspace / "gptme.toml"
    if not config_path.exists():
        return {}

    text = config_path.read_text()
    config: dict[str, str] = {}

    in_agent_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_agent_section = stripped == "[agent]"
        elif in_agent_section:
            m = re.match(r'name\s*=\s*"([^"]*)"', stripped)
            if m:
                config["agent_name"] = m.group(1)
                break

    return config


def collect_workspace_data(workspace: Path) -> dict:
    """Collect all workspace data into a dict suitable for JSON export or rendering."""
    lessons = scan_lessons(workspace)
    plugins = scan_plugins(workspace)
    packages = scan_packages(workspace)
    skills = scan_skills(workspace)
    config = read_workspace_config(workspace)

    lesson_categories: dict[str, int] = {}
    for lesson in lessons:
        cat = lesson["category"]
        lesson_categories[cat] = lesson_categories.get(cat, 0) + 1
    lesson_categories = dict(sorted(lesson_categories.items()))

    stats = {
        "total_lessons": len(lessons),
        "total_plugins": len(plugins),
        "total_packages": len(packages),
        "total_skills": len(skills),
        "lesson_categories": lesson_categories,
    }

    workspace_name = config.get("agent_name", workspace.resolve().name)

    return {
        "workspace_name": workspace_name,
        "lessons": lessons,
        "plugins": plugins,
        "packages": packages,
        "skills": skills,
        "stats": stats,
        "lesson_categories": lesson_categories,
    }


def generate(workspace: Path, output: Path, template_dir: Path | None = None) -> None:
    """Generate static HTML dashboard from workspace."""
    if template_dir is None:
        template_dir = Path(__file__).parent / "templates"

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )

    data = collect_workspace_data(workspace)

    template = env.get_template("index.html")
    html = template.render(**data)

    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(html)

    # Generate per-lesson detail pages
    lesson_template = env.get_template("lesson.html")
    for lesson in data["lessons"]:
        # Compute how many levels up from the lesson page to the site root.
        # page_url is e.g. "lessons/workflow/test.html" (depth=2), so root_prefix="../../"
        depth = len(Path(lesson["page_url"]).parts) - 1
        root_prefix = "../" * depth
        lesson_html = lesson_template.render(
            workspace_name=data["workspace_name"],
            lesson=lesson,
            body_html=render_markdown_to_html(lesson["body"]),
            root_prefix=root_prefix,
        )
        page_path = output / lesson["page_url"]
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(lesson_html)

    # Generate per-skill detail pages
    skill_template = env.get_template("skill.html")
    for skill in data["skills"]:
        # page_url is e.g. "skills/my-skill/index.html" (depth=2), so root_prefix="../../"
        depth = len(Path(skill["page_url"]).parts) - 1
        root_prefix = "../" * depth
        skill_html = skill_template.render(
            workspace_name=data["workspace_name"],
            skill=skill,
            body_html=render_markdown_to_html(skill["body"]),
            root_prefix=root_prefix,
        )
        page_path = output / skill["page_url"]
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(skill_html)

    stats = data["stats"]
    print(f"Generated dashboard at {output / 'index.html'}")
    print(
        f"  {stats['total_lessons']} lessons ({stats['total_lessons']} detail pages), "
        f"{stats['total_plugins']} plugins, "
        f"{stats['total_packages']} packages, "
        f"{stats['total_skills']} skills ({stats['total_skills']} detail pages)"
    )


def generate_json(workspace: Path, output: Path | None = None) -> str:
    """Generate JSON data dump from workspace.

    If output is provided, writes data.json to that directory.
    Returns the JSON string in all cases.
    """
    data = collect_workspace_data(workspace)
    # Exclude large fields (body, all_keywords) from JSON export — they are only
    # needed for HTML page generation and would bloat data.json unnecessarily.
    _JSON_EXCLUDE = {"body", "all_keywords"}
    export_data = {
        **data,
        "lessons": [
            {k: v for k, v in lesson.items() if k not in _JSON_EXCLUDE}
            for lesson in data["lessons"]
        ],
        "skills": [
            {k: v for k, v in skill.items() if k not in _JSON_EXCLUDE} for skill in data["skills"]
        ],
    }
    json_str = json.dumps(export_data, indent=2)

    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "data.json").write_text(json_str)
        print(f"Generated data dump at {output / 'data.json'}")

    return json_str
