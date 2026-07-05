# Agent Workspace Validator

Validates that a directory conforms to the gptme agent workspace structure.

## Purpose

This tool defines what makes a valid gptme agent workspace. It's used by:
- `agent template` CI to validate the template itself
- Agent forks to validate their workspace structure
- Developers to check workspace compliance

## Usage

### Command Line

```bash
# Validate current directory
python3 scripts/workspace-validator/validate.py

# Validate specific workspace
python3 scripts/workspace-validator/validate.py --workspace /path/to/workspace

# Run only specific checks
python3 scripts/workspace-validator/validate.py --check files,dirs

# Quiet mode (errors and warnings only)
python3 scripts/workspace-validator/validate.py --quiet
```

### As a Python Module

```python
from scripts.workspace_validator import validate_workspace
from pathlib import Path

result = validate_workspace(Path("/path/to/workspace"))
if result.passed:
    print("Workspace is valid!")
else:
    for error in result.errors:
        print(f"Error: {error}")
```

## Validation Checks

### Required Files (`--check files`)
- `gptme.toml` - Agent configuration
- `ABOUT.md` - Agent identity and background
- `README.md` - Project documentation

### Recommended Files
- `ARCHITECTURE.md` - Technical architecture
- `TASKS.md` - Task management documentation
- `GLOSSARY.md` - Terms and abbreviations

### Required Directories (`--check dirs`)
- `journal/` - Daily logs and session records
- `knowledge/` - Long-term documentation
- `lessons/` - Behavioral patterns and learnings
- `tasks/` - Task files

### Recommended Directories
- `people/` - Collaborator profiles
- `tools/` - Tool documentation
- `scripts/` - Automation scripts

### Config Validation (`--check config`)
- Parses `gptme.toml`
- Validates `[agent]` section with name
- Checks `[prompt].files` references exist

### Fork Script (`--check fork`)
- Checks `fork.sh` exists and is executable (if present)

## Exit Codes

- `0`: Validation passed (may have warnings)
- `1`: Validation failed (has errors)

## Integration with CI

### GitHub Actions

```yaml
- name: Validate workspace structure
  run: |
    python3 gptme-contrib/scripts/workspace-validator/validate.py
```

### As a Git Submodule

If your workspace uses gptme-contrib as a submodule:

```yaml
- uses: actions/checkout@v4
  with:
    submodules: recursive

- name: Validate workspace
  run: python3 gptme-contrib/scripts/workspace-validator/validate.py
```
