"""Static dashboard generator for gptme workspaces.

Scans a gptme workspace (lessons, plugins, packages, skills) and generates
a static HTML site suitable for gh-pages deployment, or a JSON data dump
for custom frontends.

Supports nested submodules: when a workspace contains git submodules with
gptme-like structure (lessons/, skills/, packages/, plugins/), their content
is automatically included with source attribution.

Designed to work with any gptme workspace (gptme-contrib, bob, alice, etc.).
"""

import configparser
import json
import re
import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

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
    return "lessons/" + Path(lesson_path).with_suffix(".html").as_posix()


def skill_page_path(skill_dir: str) -> str:
    """Convert a skill's directory path to its detail page URL path.

    E.g. 'skills/my-skill' -> 'skills/my-skill/index.html'
    """
    return (Path(skill_dir) / "index.html").as_posix()


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


def detect_submodules(workspace: Path) -> list[dict]:
    """Detect git submodules with gptme-like structure.

    Reads .gitmodules to find submodules, then checks each for
    gptme-relevant directories (lessons/, skills/, packages/, plugins/).
    """
    gitmodules = workspace / ".gitmodules"
    if not gitmodules.exists():
        return []

    config = configparser.RawConfigParser()
    config.read(str(gitmodules))

    submodules = []
    for section in config.sections():
        if not section.startswith("submodule "):
            continue

        path = config.get(section, "path", fallback=None)
        if not path:
            continue

        submodule_dir = workspace / path
        if not submodule_dir.is_dir():
            continue

        # Check for gptme-like structure
        has_lessons = (submodule_dir / "lessons").is_dir()
        has_skills = (submodule_dir / "skills").is_dir()
        has_packages = (submodule_dir / "packages").is_dir()
        has_plugins = (submodule_dir / "plugins").is_dir()

        if has_lessons or has_skills or has_packages or has_plugins:
            name = path.replace("/", "-")  # Use full path (dashes) to ensure uniqueness
            submodules.append(
                {
                    "name": name,
                    "path": path,
                    "abs_path": submodule_dir,
                    "has_lessons": has_lessons,
                    "has_skills": has_skills,
                    "has_packages": has_packages,
                    "has_plugins": has_plugins,
                }
            )

    return submodules


def scan_lessons(workspace: Path, source: str = "") -> list[dict]:
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
        if source:
            # Prefix with source name to avoid collisions when submodule has same-path lessons
            page_url = f"{source}/{page_url}"

        entry: dict = {
            "title": title,
            "category": category,
            "status": status,
            "keywords": keywords[:5],  # Limit displayed keywords
            "all_keywords": keywords,
            "body": body,
            "path": str(rel),
            "page_url": page_url,
            "kind": "lesson",
        }
        if source:
            entry["source"] = source

        lessons.append(entry)

    return lessons


def scan_plugins(
    workspace: Path,
    source: str = "",
    enabled_plugins: list[str] | None = None,
) -> list[dict]:
    """Scan plugins directory for plugin directories.

    If *enabled_plugins* is provided (from ``gptme.toml [plugins] enabled``),
    each plugin gets an ``enabled`` boolean flag.
    """
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

        # Derive the plugin module name: strip common prefix, convert hyphens
        # e.g. "gptme-consortium" -> "gptme_consortium", "user_memories" -> "user_memories"
        module_name = d.name.replace("-", "_")

        entry: dict = {
            "name": d.name,
            "description": description,
            "path": str(d.relative_to(workspace)),
        }
        if source:
            entry["source"] = source
        if enabled_plugins is not None:
            entry["enabled"] = module_name in enabled_plugins or d.name in enabled_plugins

        plugins.append(entry)

    return plugins


def scan_packages(workspace: Path, source: str = "") -> list[dict]:
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

        entry: dict = {
            "name": d.name,
            "description": description,
            "version": version,
            "path": str(d.relative_to(workspace)),
        }
        if source:
            entry["source"] = source

        packages.append(entry)

    return packages


def scan_skills(workspace: Path, source: str = "") -> list[dict]:
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
        if source:
            # Prefix with source name to avoid collisions when submodule has same-path skills
            page_url = f"{source}/{page_url}"

        entry: dict = {
            "name": name,
            "description": description,
            "body": body,
            "path": rel_dir,
            "page_url": page_url,
            "kind": "skill",
        }
        if source:
            entry["source"] = source

        skills.append(entry)

    return skills


def read_workspace_config(workspace: Path) -> dict:
    """Read gptme.toml for workspace metadata.

    Parses [agent] and [plugins] sections using tomllib for correct TOML handling.
    Returns a flat dict with keys: agent_name, plugins_enabled.
    """
    config_path = workspace / "gptme.toml"
    if not config_path.exists():
        return {}

    if tomllib is None:
        return {}

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}

    result: dict = {}
    agent = data.get("agent", {})
    if "name" in agent:
        result["agent_name"] = agent["name"]

    plugins = data.get("plugins", {})
    if "enabled" in plugins:
        result["plugins_enabled"] = plugins["enabled"]

    return result


def detect_github_url(workspace: Path) -> str:
    """Detect GitHub repository URL from git remote.

    Tries ``git remote get-url origin`` and converts SSH/HTTPS URLs to
    a browsable ``https://github.com/owner/repo`` URL.  Returns empty
    string if detection fails.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        url = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""

    # SSH: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if m:
        return f"https://github.com/{m.group(1)}"

    # HTTPS: https://github.com/owner/repo.git
    m = re.match(r"https://github\.com/(.+?)(?:\.git)?$", url)
    if m:
        return f"https://github.com/{m.group(1)}"

    return ""


def github_blob_url(gh_repo_url: str, path: str, prefix: str = "") -> str:
    """Build a GitHub blob URL for a file path.

    ``prefix`` is prepended to the path (e.g. for submodule-relative paths).
    """
    if not gh_repo_url:
        return ""
    full_path = f"{prefix}/{path}" if prefix else path
    return f"{gh_repo_url}/blob/HEAD/{full_path}"


def github_tree_url(gh_repo_url: str, path: str, prefix: str = "") -> str:
    """Build a GitHub tree URL for a directory path.

    Use this for directories (plugins, packages, skills).
    ``prefix`` is prepended to the path.
    """
    if not gh_repo_url:
        return ""
    full_path = f"{prefix}/{path}" if prefix else path
    return f"{gh_repo_url}/tree/HEAD/{full_path}"


def collect_workspace_data(workspace: Path) -> dict:
    """Collect all workspace data into a dict suitable for JSON export or rendering.

    Scans the workspace and any nested submodules with gptme-like structure.
    Items from submodules are tagged with a ``source`` field.
    Lessons and skills are merged into a unified ``guidance`` list.
    """
    config = read_workspace_config(workspace)

    lessons = scan_lessons(workspace)
    enabled_plugins = config.get("plugins_enabled")
    plugins = scan_plugins(workspace, enabled_plugins=enabled_plugins)
    packages = scan_packages(workspace)
    skills = scan_skills(workspace)

    # Scan submodules for additional content
    submodules = detect_submodules(workspace)
    submodule_names: list[str] = []
    for sub in submodules:
        sub_path: Path = sub["abs_path"]
        sub_name: str = sub["name"]
        submodule_names.append(sub_name)

        if sub["has_lessons"]:
            lessons.extend(scan_lessons(sub_path, source=sub_name))
        if sub["has_skills"]:
            skills.extend(scan_skills(sub_path, source=sub_name))
        if sub["has_packages"]:
            packages.extend(scan_packages(sub_path, source=sub_name))
        if sub["has_plugins"]:
            plugins.extend(scan_plugins(sub_path, source=sub_name))

    # Detect GitHub repo URL for source links
    gh_repo_url = detect_github_url(workspace)

    # Add GitHub source links to main-workspace items only.
    # Submodule items (source != "") belong to a different repository and would
    # get incorrect URLs if we applied the main workspace's gh_repo_url to them.
    if gh_repo_url:
        for lesson in lessons:
            if not lesson.get("source"):
                lesson["gh_url"] = github_blob_url(gh_repo_url, lesson["path"], prefix="lessons")
        for plugin in plugins:
            if not plugin.get("source"):
                plugin["gh_url"] = github_tree_url(gh_repo_url, plugin["path"])
        for pkg in packages:
            if not pkg.get("source"):
                pkg["gh_url"] = github_tree_url(gh_repo_url, pkg["path"])
        for skill in skills:
            if not skill.get("source"):
                skill["gh_url"] = github_tree_url(gh_repo_url, skill["path"])

    # Build unified guidance list (lessons + skills together)
    guidance: list[dict] = []
    for lesson in lessons:
        entry = dict(lesson)
        entry.setdefault("kind", "lesson")
        guidance.append(entry)
    for skill in skills:
        entry = dict(skill)
        entry.setdefault("kind", "skill")
        # Skills don't have category — use "skill" as category for filtering
        entry.setdefault("category", "skill")
        entry.setdefault("status", "active")
        entry.setdefault("keywords", [])
        guidance.append(entry)

    # Sort guidance: lessons first (alphabetical), then skills
    guidance.sort(key=lambda x: (x["kind"], x.get("title", x.get("name", ""))))

    lesson_categories: dict[str, int] = {}
    for item in guidance:
        cat = item.get("category", "uncategorized")
        lesson_categories[cat] = lesson_categories.get(cat, 0) + 1
    lesson_categories = dict(sorted(lesson_categories.items()))

    # Collect unique sources for UI filtering (from all content types, not just guidance)
    all_items = guidance + packages + plugins
    sources: list[str] = sorted({item.get("source", "") for item in all_items} - {""})

    stats = {
        "total_lessons": len(lessons),
        "total_plugins": len(plugins),
        "total_packages": len(packages),
        "total_skills": len(skills),
        "total_guidance": len(guidance),
        "lesson_categories": lesson_categories,
    }

    workspace_name = config.get("agent_name", workspace.resolve().name)

    return {
        "workspace_name": workspace_name,
        "gh_repo_url": gh_repo_url,
        "lessons": lessons,
        "plugins": plugins,
        "packages": packages,
        "skills": skills,
        "guidance": guidance,
        "stats": stats,
        "lesson_categories": lesson_categories,
        "submodules": submodule_names,
        "sources": sources,
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
        "guidance": [
            {k: v for k, v in item.items() if k not in _JSON_EXCLUDE} for item in data["guidance"]
        ],
    }
    json_str = json.dumps(export_data, indent=2)

    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "data.json").write_text(json_str)
        print(f"Generated data dump at {output / 'data.json'}")

    return json_str
