# gptme-dashboard

Static site generator and JSON exporter for gptme agent workspaces. Produces an HTML dashboard
suitable for GitHub Pages deployment, and a structured JSON data dump for building custom frontends.

## Purpose

Every gptme agent (Bob, Alice, etc.) and shared workspace (gptme-contrib, gptme-agent-template)
can use this tool to publish a browsable dashboard of their workspace contents — lessons, plugins,
packages, and skills — as a static site on GitHub Pages.

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

### HTML dashboard

```bash
# Generate dashboard for current workspace
gptme-dashboard --workspace .

# Custom workspace and output directory
gptme-dashboard --workspace /path/to/workspace --output _site

# Custom Jinja2 templates (complete frontend customization)
gptme-dashboard --workspace . --templates /path/to/templates
```

Output: `_site/index.html` — a single-file, zero-dependency HTML dashboard.

### JSON data dump

```bash
# Print JSON to stdout (pipe to jq, store in CI artifacts, etc.)
gptme-dashboard --workspace . --json

# Write data.json alongside HTML in the output directory
gptme-dashboard --workspace . --output _site --json
```

The JSON output contains the same data as the HTML template context, making it a
**frontend-independent data source**. Any custom frontend — React, Vue, plain JS — can
consume `data.json` directly without running the Python generator.

## What it shows

- **Lessons**: Filterable table with category, status, keywords
- **Plugins**: Name and description from README
- **Packages**: Name, version, description from pyproject.toml
- **Skills**: Name and description from SKILL.md frontmatter
- **Stats**: Counts and category distribution chart

## Requirements

- Python 3.10+
- `jinja2` (templating)
- `pyyaml` (frontmatter parsing)

## Customization

Pass `--templates` pointing to a directory with your own `index.html` (Jinja2).
The template receives these variables:

| Variable | Type | Description |
|----------|------|-------------|
| `workspace_name` | `str` | From `gptme.toml` `[agent]` name or directory name |
| `stats` | `dict` | Counts and `lesson_categories` breakdown |
| `lessons` | `list[dict]` | `title`, `category`, `status`, `keywords`, `path` |
| `plugins` | `list[dict]` | `name`, `description`, `path` |
| `packages` | `list[dict]` | `name`, `version`, `description`, `path` |
| `skills` | `list[dict]` | `name`, `description`, `path` |
| `lesson_categories` | `dict[str, int]` | Category name to lesson count (also in `stats`) |

`stats.lesson_categories` is the same value, accessible via either path.

## Deployment (GitHub Pages)

The generated `_site/` directory is ready for GitHub Pages or any static host. A typical
GitHub Actions workflow:

```yaml
- name: Build dashboard
  run: gptme-dashboard --workspace . --output _site --json
- name: Deploy to Pages
  uses: actions/upload-pages-artifact@v3
  with:
    path: _site
```

## Tests

```bash
pytest packages/gptme-dashboard/tests/ -v
```
