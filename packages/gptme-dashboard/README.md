# gptme-dashboard

Static site generator and JSON exporter for gptme agent workspaces. Produces an HTML dashboard
suitable for GitHub Pages deployment, and a structured JSON data dump for building custom frontends.

## Purpose

Every gptme agent (Bob, Alice, etc.) and shared workspace (gptme-contrib, gptme-agent-template)
can use this tool to publish a browsable dashboard of their workspace contents — lessons, skills,
plugins, and packages — as a static site on GitHub Pages.

The key design principle: **each agent owns their dashboard**. The tool generates a self-contained
static site that can be hosted anywhere. When gptme-webui gains embedding support, it will load
the agent's dashboard from a configured URL — the webui provides chrome, the agent provides content.

See [gptme-contrib#382](https://github.com/gptme/gptme-contrib/issues/382) for the full design
discussion and requirements.

## Installation

```bash
pip install gptme-dashboard
# or, from source:
uv pip install -e packages/gptme-dashboard
```

## Usage

### Generate dashboard (HTML + JSON)

```bash
# Generate dashboard for current workspace (outputs to _site/)
gptme-dashboard --workspace .

# Custom output directory
gptme-dashboard --workspace /path/to/workspace --output /path/to/_site

# Custom Jinja2 templates (complete frontend customization)
gptme-dashboard --workspace . --templates /path/to/templates
```

Both `_site/index.html` (HTML dashboard) and `_site/data.json` (structured data) are generated
together.

### Print JSON to stdout

```bash
gptme-dashboard --workspace . --json
```

Prints JSON to stdout and skips HTML generation. Pipe to `jq`, store in CI artifacts, or feed to
any custom frontend — React, Vue, plain JS — without re-running the generator.

## What it shows

- **Guidance**: Lessons and skills unified in one filterable table — category, status, keywords,
  source attribution (submodule name), and clickable detail pages with rendered markdown
- **Plugins**: Name, description, and enabled/available status from `gptme.toml`
- **Packages**: Name, version, and description from `pyproject.toml`
- **Stats**: Counts and category distribution chart

### Submodule support

When running on an agent workspace (e.g. Bob) that contains git submodules with gptme-like
structure (`lessons/`, `skills/`, `packages/`, `plugins/`, or a `gptme.toml`), the dashboard
automatically includes their content with a **Source** column showing which submodule it came from.

Typical setup — Bob's workspace containing gptme-contrib and gptme-superuser as submodules:

```bash
gptme-dashboard --workspace ~/bob
# merges lessons/skills/packages/plugins from bob, gptme-contrib, and gptme-superuser
```

## Requirements

- Python 3.10+
- `click` (CLI)
- `jinja2` (templating)
- `pyyaml` (frontmatter parsing)
- `markdown` (lesson/skill detail pages)

## Customization

Pass `--templates` with a directory containing your own `index.html` (Jinja2).
The template receives these variables:

| Variable | Type | Description |
|----------|------|-------------|
| `workspace_name` | `str` | From `gptme.toml` `[agent]` name, or directory name |
| `gh_repo_url` | `str` | Auto-detected GitHub remote URL (empty string if none) |
| `guidance` | `list[dict]` | Lessons + skills unified; each entry has `kind`, `title`, `category`, `status`, `keywords`, `path`, `source`, `gh_url` |
| `lessons` | `list[dict]` | Lesson entries only (`title`, `category`, `status`, `keywords`, `path`, `source`, `gh_url`) |
| `skills` | `list[dict]` | Skill entries only (`name`, `description`, `path`, `source`, `gh_url`) |
| `plugins` | `list[dict]` | `name`, `description`, `path`, `enabled` |
| `packages` | `list[dict]` | `name`, `version`, `description`, `path`, `gh_url` |
| `stats` | `dict` | `total_lessons`, `total_skills`, `total_guidance`, `total_plugins`, `total_packages`, `lesson_categories` |
| `lesson_categories` | `dict[str, int]` | Category → count (same as `stats.lesson_categories`) |
| `submodules` | `list[str]` | Names of detected submodules (for display/filtering) |
| `sources` | `list[str]` | Unique source labels across all content (submodule names) |

## Deployment (GitHub Pages)

The generated `_site/` directory is ready for GitHub Pages or any static host. A GitHub Actions
workflow is included in `.github/workflows/dashboard.yml` for fully automated deployment on push.
Manual workflow:

```yaml
- name: Build dashboard
  run: gptme-dashboard --workspace . --output _site
- name: Deploy to Pages
  uses: actions/upload-pages-artifact@v3
  with:
    path: _site
```

## Tests

```bash
pytest packages/gptme-dashboard/tests/ -v
```
