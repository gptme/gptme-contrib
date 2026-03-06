# gptme workspace dashboard

Static site generator that scans a gptme workspace and produces an HTML dashboard.

## Usage

```bash
# Generate dashboard for current workspace
python dashboard/generate.py

# Custom workspace and output
python dashboard/generate.py --workspace /path/to/workspace --output _site

# Custom templates
python dashboard/generate.py --templates /path/to/templates
```

## What it shows

- **Lessons**: Filterable table with category, status, keywords
- **Plugins**: Name and description from README
- **Packages**: Name, version, description from pyproject.toml
- **Skills**: Name and description from SKILL.md frontmatter
- **Stats**: Counts and category distribution chart

## Requirements

- Python 3.10+
- `jinja2` (for HTML templating)
- `pyyaml` (optional, for robust frontmatter parsing; falls back to basic parser)

## Customization

Override templates by passing `--templates` pointing to your own directory.
The template receives these variables:

| Variable | Type | Description |
|----------|------|-------------|
| `workspace_name` | str | From gptme.toml agent name or directory name |
| `stats` | dict | Counts: total_lessons, total_plugins, etc. |
| `lessons` | list[dict] | title, category, status, keywords, path |
| `plugins` | list[dict] | name, description, path |
| `packages` | list[dict] | name, version, description, path |
| `skills` | list[dict] | name, description, path |
| `lesson_categories` | dict[str, int] | Category name to count |

## Deployment

The generated `_site/` directory is ready for GitHub Pages or any static host.

## Tests

```bash
pytest dashboard/tests/ -v
```
