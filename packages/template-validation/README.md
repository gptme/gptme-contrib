# Template Validation

Validation tools for gptme-agent-template forks.

## Overview

This package provides validation tools to ensure clean template separation and proper agent identity configuration. It supports two modes:

- **Template mode**: Validates the template itself stays clean (no agent references, no incomplete patterns)
- **Fork mode**: Validates forked agents have replaced template-specific patterns while allowing documentation references

## Installation

```bash
cd gptme-contrib
uv pip install -e packages/template-validation
```

## Usage

### Command Line

```bash
# Fork validation (default)
template-validation check-names

# Template validation
template-validation check-names --template-mode

# With custom exclusions
template-validation check-names --exclude custom/ --exclude *.bak

# Strict mode (validate agent identity)
template-validation check-names --strict

# With suggestions
template-validation check-names --suggest

# Using config file
template-validation check-names --config .template-validation.yaml
```

### Config File

Create `.template-validation.yaml`:

```yaml
mode: fork
excludes:
  - my-custom-docs/
  - experimental/
patterns:
  my-pattern: "CUSTOM_REGEX"
```

### As Library

```python
from template_validation import validate_names, validate_agent_identity

# Validate naming patterns
result = validate_names(root=Path("."), mode="fork")
if not result.is_valid():
    print(result.format_report())
    
# Validate agent identity
errors = validate_agent_identity(Path("."))
if errors:
    for error in errors:
        print(f"Error: {error}")
```

### Pre-commit Hook

For template repository (`.pre-commit-config.yaml`):

```yaml
- repo: local
  hooks:
    - id: check-names
      name: Check template names
      entry: uv run template-validation check-names --template-mode
      language: system
```

For fork validation script:

```bash
# In fork.sh
uv run template-validation check-names --fork-mode
```

## Validation Rules

### Template Mode

Checks for:
- Incomplete agent references (`gptme-agent` without `-template`)
- Placeholder names (`agent-name`, `[AGENT_NAME]`, `[YOUR_NAME]`)

### Fork Mode

Checks for:
- Template references (`gptme-agent-template`)
- Template suffix patterns (`-template`)

Automatically excludes:
- Documentation directories (`docs/`, `knowledge/`, `journal/`, `lessons/`, `skills/`)
- Markdown files (`*.md`)

### Strict Mode

Additionally validates:
- `gptme.toml` has `[agent]` section with non-default name
- `ABOUT.md` exists and doesn't contain placeholders

## Development

Run tests:

```bash
cd packages/template-validation
uv pip install -e ".[dev]"
pytest
```

Run with coverage:

```bash
pytest --cov=template_validation --cov-report=term-missing
```

## Design Rationale

The validation tool distinguishes between template validation (ongoing) and fork validation (one-time):

- **Template validation** ensures the template repository stays clean for future forks
- **Fork validation** allows documentation to reference the template while ensuring code is properly updated

This matches the actual usage pattern: agents need to document their relationship to the template, but shouldn't have template-specific code references.

## License

MIT
