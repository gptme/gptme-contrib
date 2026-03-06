#!/usr/bin/env python3
"""Static dashboard generator for gptme workspaces.

Scans a gptme workspace (lessons, plugins, packages, skills) and generates
a static HTML site suitable for gh-pages deployment.

Usage:
    python dashboard/generate.py [--workspace .] [--output _site]

Designed to work with any gptme workspace (gptme-contrib, bob, alice, etc.).
"""

import argparse
import re
import sys
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    print("jinja2 required: pip install jinja2", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


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

    if yaml:
        try:
            fm = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError:
            fm = {}
    else:
        # Minimal fallback parser for key: value pairs
        fm = {}
        for line in fm_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"').strip("'")

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

        keywords = []
        match = fm.get("match", {})
        if isinstance(match, dict):
            keywords = match.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]

        lessons.append(
            {
                "title": title,
                "category": category,
                "status": status,
                "keywords": keywords[:5],  # Limit displayed keywords
                "path": str(rel),
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
            # First non-empty, non-heading line
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
            m = re.search(r'description\s*=\s*"([^"]*)"', text)
            if m:
                description = m.group(1)
            m = re.search(r'version\s*=\s*"([^"]*)"', text)
            if m:
                version = m.group(1)

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

        skills.append(
            {
                "name": name,
                "description": description,
                "path": str(skill_md.parent.relative_to(workspace)),
            }
        )

    return skills


def read_workspace_config(workspace: Path) -> dict:
    """Read gptme.toml for workspace metadata."""
    config_path = workspace / "gptme.toml"
    if not config_path.exists():
        return {}

    text = config_path.read_text()
    config = {}

    m = re.search(r'name\s*=\s*"([^"]*)"', text)
    if m:
        config["agent_name"] = m.group(1)

    return config


def generate(workspace: Path, output: Path, template_dir: Path | None = None) -> None:
    """Generate static dashboard from workspace."""
    if template_dir is None:
        template_dir = Path(__file__).parent / "templates"

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )

    # Scan workspace
    lessons = scan_lessons(workspace)
    plugins = scan_plugins(workspace)
    packages = scan_packages(workspace)
    skills = scan_skills(workspace)
    config = read_workspace_config(workspace)

    # Compute stats
    lesson_categories: dict[str, int] = {}
    for lesson in lessons:
        cat = lesson["category"]
        lesson_categories[cat] = lesson_categories.get(cat, 0) + 1

    stats = {
        "total_lessons": len(lessons),
        "total_plugins": len(plugins),
        "total_packages": len(packages),
        "total_skills": len(skills),
        "lesson_categories": dict(sorted(lesson_categories.items())),
    }

    # Determine workspace name
    workspace_name = config.get("agent_name", workspace.resolve().name)

    # Render
    template = env.get_template("index.html")
    html = template.render(
        workspace_name=workspace_name,
        stats=stats,
        lessons=lessons,
        plugins=plugins,
        packages=packages,
        skills=skills,
        lesson_categories=lesson_categories,
    )

    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(html)
    print(f"Generated dashboard at {output / 'index.html'}")
    print(
        f"  {stats['total_lessons']} lessons, "
        f"{stats['total_plugins']} plugins, "
        f"{stats['total_packages']} packages, "
        f"{stats['total_skills']} skills"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate static gptme workspace dashboard"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Path to gptme workspace (default: current directory)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_site"),
        help="Output directory (default: _site)",
    )
    parser.add_argument(
        "--templates",
        type=Path,
        default=None,
        help="Custom template directory (default: dashboard/templates/)",
    )
    args = parser.parse_args()

    generate(args.workspace, args.output, args.templates)


if __name__ == "__main__":
    main()
