---
status: active
match:
  keywords:
    - ci failure
    - pre-commit
    - hook failure
    - ci fix
---

# CI Failure Resolution and Pre-commit Infrastructure Maintenance

## Rule
Systematically resolve CI failures by identifying failure patterns, applying targeted fixes, and restoring infrastructure integrity while maintaining development velocity.

## Context
When encountering pre-commit CI failures in autonomous sessions that block commits or indicate infrastructure degradation.

## Detection
Observable signals indicating CI/pre-commit issues:
- Pre-commit workflow runs failing on master branch
- `git commit` failing with pre-commit hook errors
- Multiple consecutive CI failures with similar error patterns
- Corrupted or missing pre-commit scripts
- Partial markdown files (empty headers at end)
- Broken internal links in documentation
- Typing errors (mypy/ruff) blocking commits
- Task metadata inconsistencies (wrong field names/formats)

Common failure patterns:
- `markdownlint-cli2` errors about empty headers
- `check-links` errors about broken internal references
- `ruff-format` errors about quote style inconsistencies
- `mypy` typing errors in scripts
- Missing or corrupted pre-commit scripts

## Pattern

### Phase 1: Diagnosis (2-3 minutes)

```bash
# 1. Check recent CI failure patterns
gh run list --limit 10
gh run view <run-id> --log-failed

# 2. Run pre-commit locally to identify specific failures
pre-commit run --all-files

# 3. Categorize failures
echo "=== Failure Categories ==="
echo "1. Content issues (partial markdown, broken links)"
echo "2. Code quality (typing, formatting)"
echo "3. Infrastructure (missing scripts, configuration)"
```

### Phase 2: Systematic Resolution

**Content Issues** (markdown, links):
```bash
# Find partial markdown files (empty headers)
grep -rE "^#+ $"

# Fix by removing empty trailing headers
# Or completing incomplete sections

# Find broken internal links
# Use pre-commit check-links output
```

**Code Quality Issues** (typing, formatting):
```bash
# Fix ruff formatting issues
ruff format .

# Fix mypy typing errors one by one
# Common patterns:
# - List[str] → list[str] (Python 3.9+)
# - Dict[str, int] → dict[str, int]
# - Missing Optional imports
# - E402 import order issues
```

**Infrastructure Issues** (missing scripts):
```bash
# Restore from last known good commit
git log --oneline -- scripts/precommit/
git show <last-good-commit>:scripts/precommit/<script>.py > scripts/precommit/<script>.py

# Or update pyproject.toml exclusions if needed
# Use a proper editor or sed — do NOT append with echo, as it will
# create a duplicate [tool.mypy] section header if one already exists
# (invalid TOML that breaks all tooling reading pyproject.toml)
# Example (only if [tool.mypy] section doesn't already exist):
python -c "
import sys, re
content = open('pyproject.toml').read()
if '[tool.mypy]' not in content:
    open('pyproject.toml', 'a').write('\n[tool.mypy]\nexclude = [\"scripts/precommit\"]\n')
else:
    print('ERROR: [tool.mypy] section already exists — edit it manually', file=sys.stderr)
    sys.exit(1)
"
```

### Phase 3: Metadata Standardization

When fixing task files:
```bash
# Standardize field names
# Wrong: status: completed
# Right: state: done

# Standardize date formats
# Wrong: completed_date: 2026-02-08
# Right: updated: 2026-02-08T21:36:00+00:00

# Ensure all task files have required frontmatter
# - title
# - state
# - created
# - updated
# - tags
```

### Phase 4: Verification

```bash
# Run full pre-commit suite
pre-commit run --all-files

# Verify CI status
gh run list --limit 5

# Commit fixes if clean
git commit <files> -m "fix: resolve [specific issue]"
```

## Anti-Patterns

**Wrong: Ignore CI failures**
```bash
# Don't bypass pre-commit
git commit --no-verify  # Bypasses quality checks

# Don't leave failing CI on master
# Each failure can mask new issues
```

**Wrong: Fix symptoms only**
```bash
# Don't just delete failing checks
# Fix the underlying issue

# Don't just remove broken links
# Find where they should point or remove the reference
```

**Wrong: Broad fixes**
```bash
# Don't run broad formatting without review
ruff check --unsafe-fixes  # Can introduce issues (note: --unsafe-fixes is a ruff check flag, not ruff format)

# Don't auto-fix without understanding
pre-commit run --all-files --show-diff-on-failure
# Review each change before committing
```

## Success Example

**Scenario**: Multiple CI failures on master branch

**Failures Identified**:
1. Partial markdown files (empty headers)
2. Broken internal links to non-existent files
3. Task metadata inconsistencies
4. Pre-commit script corruption
5. Typing errors in restored scripts

**Resolution Sequence**:
1. Fixed 6 partial markdown files by removing empty trailing headers
2. Fixed broken links to non-existent paths
3. Standardized task metadata: `status: completed` → `state: done` with ISO datetime
4. Restored 4 corrupted pre-commit scripts from last known good commit
5. Fixed typing issues: `List` → `list`, E402 import order, `Optional` annotations

**Outcome**: All pre-commit checks passing, clean CI status restored

## Recovery Patterns

**From Corrupted Scripts**:
```bash
# Find last known good version
git log --oneline -- scripts/precommit/
git show <commit>:scripts/precommit/script.py > /tmp/good_script.py
cp /tmp/good_script.py scripts/precommit/script.py
```

**From Persistent Failures**:
```bash
# If pre-commit keeps failing after fixes:
pre-commit clean  # Clear pre-commit cache
pre-commit install --force  # Reinstall hooks
```

## Outcome
Following this pattern results in:
- **Clean CI status**: Master branch always green
- **Infrastructure integrity**: Pre-commit scripts reliable
- **Quality maintenance**: Code standards enforced
- **Development velocity**: No blocking CI issues
- **Documentation health**: No broken links or partial files

## Session Integration

**During Autonomous Sessions**:
1. Always check CI status in Phase 1
2. If CI failing → Fix before starting new work
3. Document fixes in journal with specific failure patterns
4. Extract lessons from complex resolution sequences

**Time Allocation**:
- Diagnosis: 2-3 minutes
- Resolution: 10-15 minutes (depending on complexity)
- Verification: 2-3 minutes
- Total: 15-20 minutes to restore clean status

## Tools

**pre-commit** is the standard hook runner used in most gptme-agent workspaces. However, **`prek`** is a superior Rust-based alternative that Bob uses — it supports nested configs, parallelism, and is significantly faster. If your workspace supports it, prefer `prek run --all-files` over `pre-commit run --all-files`.

## Related
- [Git Workflow](./git-workflow.md) - Proper commit practices
- [Autonomous Session Structure](../autonomous/autonomous-session-structure.md) - Phase 1 CI checks
- [Strategic Focus During Autonomous Sessions](../autonomous/strategic-focus-during-autonomous-sessions.md) - Prioritizing infrastructure health

## Origin
Extracted from resolving multiple CI/pre-commit issues in agent workspaces: script restoration, typing fixes, metadata standardization, and content cleanup. Common failure modes appear across agents maintaining similar infrastructure.
