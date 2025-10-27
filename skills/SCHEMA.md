# Skill Metadata Schema

Skills use YAML frontmatter in their SKILL.md file with the following schema:

## Required Fields

```yaml
---
name: skill-name           # Unique identifier (kebab-case)
keywords:                  # Keywords for auto-inclusion
  - keyword1
  - keyword2
description: |             # Brief description (1-2 sentences)
  What this skill does
tools:                     # List of tool scripts provided
  - script-name
---
```

## Optional Fields

```yaml
version: "1.0.0"           # Semantic version
author: "Author Name"      # Skill creator
status: active             # active, experimental, deprecated
dependencies:              # Required tools/packages
  - tool-name
  - package-name
examples:                  # Paths to example files
  - examples/example1.md
  - examples/example2.md
related:                   # Related skills or lessons
  - other-skill
  - lessons/some-lesson.md
```

## Complete Example

```yaml
---
name: data-analysis
keywords:
  - data
  - analysis
  - pandas
  - visualization
description: |
  Analyze datasets using pandas, create visualizations,
  and extract insights from structured data.
tools:
  - analyze.py
  - visualize.py
version: "1.0.0"
author: "Bob"
status: active
dependencies:
  - pandas
  - matplotlib
  - seaborn
examples:
  - examples/csv-analysis.md
  - examples/time-series.md
related:
  - web-scraping
  - lessons/tools/python.md
---
```

## Field Definitions

### name
- Type: string (kebab-case)
- Required: Yes
- Unique identifier for the skill
- Used in CLI commands and references

### keywords
- Type: array of strings
- Required: Yes
- Triggers auto-inclusion when mentioned
- Should be specific but not too narrow
- 3-5 keywords recommended

### description
- Type: string (multiline)
- Required: Yes
- 1-2 sentence summary
- Explains what the skill does
- Used in skill listings

### tools
- Type: array of strings
- Required: Yes
- List of executable scripts in scripts/ directory
- Used for tool discovery and documentation

### version
- Type: string (semver)
- Optional: Yes
- Semantic versioning (major.minor.patch)
- Helps track skill evolution

### author
- Type: string
- Optional: Yes
- Creator or maintainer name
- Contact information can be included

### status
- Type: enum (active, experimental, deprecated)
- Optional: Yes (defaults to active)
- active: Production-ready
- experimental: Under development
- deprecated: Being replaced

### dependencies
- Type: array of strings
- Optional: Yes
- External tools or packages required
- Used for validation and setup

### examples
- Type: array of strings (paths)
- Optional: Yes
- Paths to example files (relative to skill directory)
- Helps users understand usage

### related
- Type: array of strings (paths or skill names)
- Optional: Yes
- Links to related skills or lessons
- Helps with discovery and learning

## Validation

Skills can be validated with:
```bash
# Future CLI command
gptme skill validate <skill-name>
```

## Comparison to Lesson Schema

**Similarities**:
- Both use YAML frontmatter
- Both use keywords for auto-inclusion
- Both have name and description

**Differences**:
- Skills have `tools` field (lessons don't provide tools)
- Skills have `dependencies` (external requirements)
- Skills have `examples` directory structure
- Lessons have `status` for lifecycle (skills use status differently)

## Evolution

Schema may evolve based on:
- Phase 4.2-4.3 implementation learnings
- Real-world skill creation patterns
- Integration needs with gptme core
- Cursor rules compatibility (Phase 5)
