---
name: template-skill
description: Template for creating new skills in gptme-contrib. Use this as a starting point when creating your own skills.

# Exchange fields (optional — fill in when publishing to a registry)
# exchange:
#   version: "1.0.0"         # semantic version
#   author: null             # GitHub handle
#   license: null            # e.g. MIT
#   category: null           # e.g. data-engineering
#   dependencies:
#     skills: []             # other skill names this skill depends on
#     tools: []              # gptme tool requirements (e.g. [shell, python])
#     packages: []           # Python packages (e.g. [pandas, matplotlib])
#   quality:
#     usage_count: 0
#     success_rate: null     # filled by gptme-sessions telemetry
#     loo_delta: null        # filled by lesson-loo-analysis.py
#   provenance:
#     source_repo: null      # e.g. TimeToBuildBob/bob
#     source_path: null      # e.g. skills/my-skill
---

# Template Skill

This is a minimal skill template demonstrating the basic structure of a gptme skill.

## Overview

Skills are enhanced lessons that bundle:
- Instructional content (like lessons)
- Executable scripts and utilities (optional)
- Dependencies and setup requirements (optional)
- Hook points for automation (optional)

## Basic Structure

Every skill needs:
1. **SKILL.md** - This file with YAML frontmatter + Markdown content
2. **Supporting files** (optional) - Scripts, templates, or resources

## YAML Frontmatter

Required fields:
- `type: skill` - Distinguishes from lessons
- `name: skill-name` - Skill identifier (must match directory name)
- `description: ...` - What the skill does and when to use it
- `status: active` - active, automated, deprecated, or archived
- `match: {keywords: [...]}` - Trigger keywords

Optional fields:
- `scripts: []` - List of bundled Python scripts
- `dependencies: []` - Required Python packages
- `hooks: []` - Execution hooks (future feature)

Optional exchange fields (for publishing to a skill registry):
- `exchange.version` - Semantic version string (e.g. `"1.0.0"`)
- `exchange.author` - GitHub handle of the skill author
- `exchange.license` - License identifier (e.g. `MIT`)
- `exchange.category` - Skill category (e.g. `data-engineering`, `devops`)
- `exchange.dependencies.skills` - Other skill names this skill requires
- `exchange.dependencies.tools` - gptme tools required (e.g. `[shell, python]`)
- `exchange.dependencies.packages` - Python packages required
- `exchange.quality.usage_count` - Number of times invoked (filled by telemetry)
- `exchange.quality.success_rate` - Fraction of successful invocations (telemetry)
- `exchange.quality.loo_delta` - Leave-one-out quality delta (lesson-loo-analysis.py)
- `exchange.provenance.source_repo` - Origin repo (e.g. `TimeToBuildBob/bob`)
- `exchange.provenance.source_path` - Path within source repo

## Markdown Content

The markdown body can include:
- Detailed instructions for the LLM
- Step-by-step workflows
- Code examples and templates
- Best practices and principles
- References to supporting files

## Creating Your Own Skill

1. Copy this template-skill directory
2. Rename to your-skill-name
3. Update SKILL.md frontmatter (especially name and description)
4. Write your skill instructions in markdown
5. Add any supporting scripts or resources
6. Test with gptme

## Example: Minimal Skill

```yaml
---
type: skill
name: my-skill
description: Brief description of what the skill does
status: active
match:
  keywords: [keyword1, keyword2]
scripts: []
dependencies: []
---

# My Skill

Instructions for using this skill...
```

## Example: Skill with Scripts

```yaml
---
type: skill
name: data-analysis
description: Data analysis workflows with pandas and visualization
status: active
match:
  keywords: [data analysis, pandas, visualization]
scripts:
  - helpers.py
  - plot_utils.py
dependencies:
  - pandas
  - matplotlib
---

# Data Analysis Skill

Use this skill for data analysis tasks...

## Bundled Scripts

- `helpers.py`: Common data manipulation functions
- `plot_utils.py`: Visualization utilities

## Usage

```python
# Import bundled helpers
from helpers import load_data, clean_data
from plot_utils import plot_distribution

# Analyze data
df = load_data("data.csv")
df = clean_data(df)
plot_distribution(df["column"])
```
```

## Example: Publishable Skill

```yaml
---
name: postgres-query-optimizer
description: Analyze and rewrite slow PostgreSQL queries using EXPLAIN ANALYZE output
status: active
match:
  keywords: [postgres, sql, query, explain, slow query]
exchange:
  version: "1.0.0"
  author: TimeToBuildBob
  license: MIT
  category: data-engineering
  dependencies:
    skills: []
    tools: [shell]
    packages: []
  quality:
    usage_count: 0
    success_rate: null
    loo_delta: null
  provenance:
    source_repo: TimeToBuildBob/bob
    source_path: skills/postgres-query-optimizer
---
```

## Integration with Lessons

Skills complement lessons:
- **Lessons**: Behavioral patterns and best practices (auto-included)
- **Skills**: Executable workflows with bundled tools (explicitly loaded)

Example:
- Lesson teaches: "Use type hints in Python"
- Skill provides: Type checking utilities and templates

## Publishing and Exchange

The `exchange:` block makes a skill discoverable and installable across the fleet.
When all agents share a registry (e.g. `gptme-contrib/skills/registry.json`), any
agent can find skills by category or dependency and install them with one command.

Exchange fields are optional — a skill works fine without them. Add them when you
want the skill to be findable by other agents or to track quality signals over time.

Quality fields (`usage_count`, `success_rate`, `loo_delta`) are filled automatically
by `gptme-sessions` telemetry and `lesson-loo-analysis.py` — you don't need to fill
them in by hand.

## Related

- [Skills README](../README.md) - Skills system overview
