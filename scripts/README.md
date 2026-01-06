# Check Names Script

Simple validation script for gptme-agent-template and its forks.

## Usage

```bash
# Auto-detect mode (based on git remote)
./scripts/check-names.sh

# Explicit mode
./scripts/check-names.sh template  # Check template has no instance names
./scripts/check-names.sh fork      # Check fork has no template references
```

## Modes

### Template Mode
Validates the template repository has no instance-specific names (bob, alice).

**Excludes**: Scripts and files that legitimately reference agent names.

### Fork Mode
Validates forked agent repositories have no "gptme-agent-template" references in code.

**Auto-excludes**:
- Documentation directories: `docs/`, `knowledge/`, `journal/`, `lessons/`, `skills/`
- Markdown files: `*.md`
- Git hooks: `dotfiles/.config/git/hooks/`
- Monitoring scripts: `scripts/github/`

## Integration

### Pre-commit Hook

For the template repository:
```yaml
# .pre-commit-config.yaml
- repo: local
  hooks:
    - id: check-names
      name: Check template naming
      entry: bash scripts/check-names.sh template
      language: system
      pass_filenames: false
```

### Fork Validation

Run during fork creation:
```bash
bash scripts/check-names.sh fork
```
