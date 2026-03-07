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
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import markdown  # type: ignore[import-untyped]
import yaml  # type: ignore[import-untyped]
from jinja2 import Environment, FileSystemLoader


def _preprocess_markdown(md_text: str) -> str:
    """Ensure blank lines before list blocks so the markdown library renders them.

    Python's ``markdown`` library follows the original Markdown spec which requires
    a blank line before a list.  Without it, ``- item`` after a paragraph is treated
    as continuation text, not a list.  This preprocessor inserts blank lines where
    needed.
    """
    lines = md_text.split("\n")
    result: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        is_list = stripped.startswith(("- ", "* ", "+ ")) or (
            len(stripped) > 2 and stripped[0].isdigit() and ". " in stripped[:5]
        )
        if is_list and i > 0:
            prev = lines[i - 1].strip()
            # Insert blank line if previous line is non-empty and not itself a list item
            if (
                prev
                and not prev.startswith(("- ", "* ", "+ "))
                and not (len(prev) > 2 and prev[0].isdigit() and ". " in prev[:5])
            ):
                result.append("")
        result.append(line)
    return "\n".join(result)


def render_markdown_to_html(md_text: str) -> str:
    """Render markdown text to HTML using the markdown library."""
    preprocessed = _preprocess_markdown(md_text)
    return str(
        markdown.markdown(
            preprocessed,
            extensions=["fenced_code", "tables", "codehilite"],
            extension_configs={"codehilite": {"css_class": "code", "noclasses": True}},
        )
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


def _parse_toml(path: Path) -> dict:
    """Parse a TOML file, returning an empty dict on failure.

    Returns an empty dict silently when the file does not exist.
    Logs a warning to stderr when the file exists but contains a syntax error.
    """
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            print(
                "Warning: tomli is not installed; [agent.urls] and other TOML features unavailable"
                " (install gptme-dashboard[tomli] or upgrade to Python 3.11+)",
                file=sys.stderr,
            )
            return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)  # type: ignore[no-any-return]
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        print(f"Warning: {path}: TOML parse error — {exc}", file=sys.stderr)
        return {}


def read_agent_urls(workspace: Path) -> dict[str, str]:
    """Read [agent.urls] from gptme.toml.

    Returns a dict of link name → URL, e.g. ``{"dashboard": "https://...", "repo": "..."}``.
    Returns an empty dict if the section is absent or gptme.toml is missing.

    Note: ``[agent.urls]`` is not yet part of gptme's ``AgentConfig`` schema, so we
    parse gptme.toml directly rather than going through ``get_project_config``.
    """
    data = _parse_toml(workspace / "gptme.toml")
    links = data.get("agent", {}).get("urls", {})
    if isinstance(links, dict):
        safe_links: dict[str, str] = {}
        for key, value in links.items():
            if not isinstance(value, str):
                continue
            url = value.strip()
            parsed = urlparse(url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                safe_links[str(key)] = url
        return safe_links
    return {}


def _safe_grade(val: object, default: float = 0.0) -> float:
    """Convert *val* to a rounded float grade, returning *default* on failure."""
    try:
        return round(float(val), 2)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _safe_int(val: object, default: int = 0) -> int:
    """Convert *val* to int, returning *default* on failure (e.g. None or non-numeric)."""
    try:
        return int(val)  # type: ignore[call-overload, no-any-return]
    except (TypeError, ValueError):
        return default


def scan_recent_sessions(workspace: Path, days: int = 30) -> list[dict]:
    """Scan recent agent sessions using gptme-sessions discovery.

    Returns sessions run in *workspace* over the past *days* days, sorted by
    date descending (most recent first).  Returns an empty list if
    gptme-sessions is not installed or no matching sessions are found.

    Each session dict contains:
    ``name``, ``date``, ``harness``, ``commits``, ``edits``, ``errors``,
    ``grade``, ``category``.

    Workspace filtering
    -------------------
    - **gptme**: sessions whose ``config.toml`` workspace field matches
      *workspace* are included.  Sessions without a workspace field (older
      sessions) are always included.
    - **Claude Code**: sessions whose CC project directory name (an encoded
      workspace path) matches *workspace* are included.
    """
    try:
        from gptme_sessions.discovery import (
            decode_cc_project_path,
            discover_cc_sessions,
            discover_gptme_sessions,
            parse_gptme_config,
        )
        from gptme_sessions.signals import extract_from_path
    except ImportError:
        print(
            "warning: --sessions requested but gptme-sessions is not installed; "
            "install it with: pip install gptme-dashboard[sessions]",
            file=sys.stderr,
        )
        return []

    end = date.today()
    start = end - timedelta(days=days)
    workspace_resolved = workspace.resolve()

    sessions: list[dict] = []

    print("Scanning sessions...", end="", flush=True, file=sys.stderr)
    gptme_count = 0
    cc_count = 0

    # --- gptme sessions ---
    for session_dir in discover_gptme_sessions(start, end):
        config = parse_gptme_config(session_dir)
        session_ws = config.get("workspace", "")
        # Include when workspace matches or when no workspace metadata is available.
        if session_ws:
            session_ws_path = Path(session_ws).resolve()
            if not (
                session_ws_path == workspace_resolved
                or session_ws_path.is_relative_to(workspace_resolved)
                or workspace_resolved.is_relative_to(session_ws_path)
            ):
                continue

        jsonl = session_dir / "conversation.jsonl"
        if not jsonl.exists():
            continue

        try:
            signals = extract_from_path(jsonl)
        except Exception:
            signals = {}

        date_str = session_dir.name[:10]
        try:
            date.fromisoformat(date_str)
        except ValueError:
            continue  # Directory name doesn't start with a valid ISO date — skip

        gptme_count += 1
        if gptme_count % 50 == 0:
            print(f" {gptme_count} gptme", end="", flush=True, file=sys.stderr)

        sessions.append(
            {
                "name": session_dir.name,
                "date": date_str,
                "harness": "gptme",
                "commits": len(signals.get("git_commits", [])),
                "edits": len(set(signals.get("file_writes", []))),
                "errors": _safe_int(signals.get("error_count", 0)),
                "grade": _safe_grade(signals.get("grade", 0.0)),
                "category": signals.get("inferred_category", ""),
            }
        )

    # --- Claude Code sessions ---
    for jsonl in discover_cc_sessions(start, end):
        # The CC project directory name is the workspace path with '/' → '-'.
        project_dir_name = jsonl.parent.name
        decoded = decode_cc_project_path(project_dir_name)
        decoded_path = Path(decoded).resolve()
        if not (
            decoded_path == workspace_resolved
            or decoded_path.is_relative_to(workspace_resolved)
            or workspace_resolved.is_relative_to(decoded_path)
        ):
            continue

        try:
            session_date = str(date.fromtimestamp(os.path.getmtime(jsonl)))
        except (OSError, ValueError):
            continue  # No usable date — skip this session

        try:
            signals = extract_from_path(jsonl)
        except Exception:
            signals = {}

        cc_count += 1
        if cc_count % 50 == 0:
            print(f" {cc_count} cc", end="", flush=True, file=sys.stderr)

        sessions.append(
            {
                "name": jsonl.stem[:32],
                "date": session_date,
                "harness": "claude-code",
                "commits": len(signals.get("git_commits", [])),
                "edits": len(set(signals.get("file_writes", []))),
                "errors": _safe_int(signals.get("error_count", 0)),
                "grade": _safe_grade(signals.get("grade", 0.0)),
                "category": signals.get("inferred_category", ""),
            }
        )

    total_found = len(sessions)
    sessions.sort(key=lambda s: s["date"], reverse=True)
    sessions = sessions[:50]
    cap_msg = f", showing {len(sessions)}" if total_found > len(sessions) else ""
    print(
        f" done ({gptme_count} gptme + {cc_count} claude-code = {total_found} matching{cap_msg})",
        file=sys.stderr,
    )
    return sessions


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
    """Read gptme.toml for workspace metadata using inline TOML parsing."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[import-not-found]

    toml_path = workspace / "gptme.toml"
    if not toml_path.exists():
        return {}

    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}

    config: dict = {}

    agent = data.get("agent", {})
    if isinstance(agent, dict) and agent.get("name"):
        config["agent_name"] = agent["name"]

    plugins = data.get("plugins", {})
    if isinstance(plugins, dict) and plugins.get("enabled"):
        config["plugins_enabled"] = list(plugins["enabled"])

    return config


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
    except (OSError, subprocess.TimeoutExpired):
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


def collect_workspace_data(
    workspace: Path,
    include_sessions: bool = False,
    sessions_days: int = 30,
) -> dict:
    """Collect all workspace data into a dict suitable for JSON export or rendering.

    Scans the workspace and any nested submodules with gptme-like structure.
    Items from submodules are tagged with a ``source`` field.
    Lessons and skills are merged into a unified ``guidance`` list.

    Parameters
    ----------
    workspace:
        Path to the gptme workspace root.
    include_sessions:
        When *True*, scan recent agent sessions via gptme-sessions and include
        them in the returned dict under the ``"sessions"`` key.  Requires the
        ``gptme-sessions`` package to be installed; returns an empty list when
        it is absent.
    sessions_days:
        How many days back to scan for sessions (default 30).
    """
    config = read_workspace_config(workspace)
    agent_urls = read_agent_urls(workspace)

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

    # Optionally scan recent sessions
    sessions: list[dict] = []
    if include_sessions:
        sessions = scan_recent_sessions(workspace, days=sessions_days)

    stats = {
        "total_lessons": len(lessons),
        "total_plugins": len(plugins),
        "total_packages": len(packages),
        "total_skills": len(skills),
        "total_guidance": len(guidance),
        "total_sessions": len(sessions),
        "lesson_categories": lesson_categories,
    }

    workspace_name = config.get("agent_name", workspace.resolve().name)

    return {
        "workspace_name": workspace_name,
        "gh_repo_url": gh_repo_url,
        "agent_urls": agent_urls,
        "lessons": lessons,
        "plugins": plugins,
        "packages": packages,
        "skills": skills,
        "guidance": guidance,
        "sessions": sessions,
        "stats": stats,
        "lesson_categories": lesson_categories,
        "submodules": submodule_names,
        "sources": sources,
    }


def generate(
    workspace: Path,
    output: Path,
    template_dir: Path | None = None,
    include_sessions: bool = False,
    sessions_days: int = 30,
) -> dict:
    """Generate static HTML dashboard from workspace.

    Returns the collected workspace data dict so callers can reuse it
    (e.g. for JSON export) without rescanning.
    """
    if template_dir is None:
        template_dir = Path(__file__).parent / "templates"

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )

    data = collect_workspace_data(
        workspace, include_sessions=include_sessions, sessions_days=sessions_days
    )

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
    session_msg = f", {stats['total_sessions']} sessions" if include_sessions else ""
    print(f"Generated dashboard at {output / 'index.html'}")
    print(
        f"  {stats['total_lessons']} lessons ({stats['total_lessons']} detail pages), "
        f"{stats['total_plugins']} plugins, "
        f"{stats['total_packages']} packages, "
        f"{stats['total_skills']} skills ({stats['total_skills']} detail pages)"
        f"{session_msg}"
    )

    return data


def generate_json(
    workspace: Path,
    output: Path | None = None,
    include_sessions: bool = False,
    sessions_days: int = 30,
    _data: dict | None = None,
) -> str:
    """Generate JSON data dump from workspace.

    If output is provided, writes data.json to that directory.
    Returns the JSON string in all cases.

    If *_data* is provided, reuse it instead of rescanning the workspace.
    """
    data = (
        _data
        if _data is not None
        else collect_workspace_data(
            workspace, include_sessions=include_sessions, sessions_days=sessions_days
        )
    )
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
