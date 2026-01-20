---
match:
  keywords:
    - "CI secret not available"
    - "fork PR failing CI"
    - "secret not accessible via fork"
    - "push directly to repo"
    - "ANTHROPIC_API_KEY not set"
    - "permission denied pushing to org repo"
---

# Fork PR Secrets Access

## Rule
Secrets are not available to PRs from forks; push directly to the repo if you have member access.

## Context
When CI fails on a PR due to missing secrets, and you're a member of the organization.

## Detection
Observable signals:
- CI fails with "secret is empty" or "API key not found" errors
- You created PR from a fork (your-username/repo → org/repo)
- CI succeeds on master but fails on your PR with same code
- Error messages like "No API key found, couldn't auto-detect provider"
- You have org membership but PR still can't access secrets

## Pattern
Check access and push directly:
```shell
# Check your permissions on the repo
gh api repos/ORG/REPO --jq '.permissions'
# If push: true → you can push directly

# Option 1: Push directly to the org repo
git remote add upstream git@github.com:ORG/REPO.git
git push upstream your-branch:your-branch

# Option 2: For existing fork-based PR with no push access
# Have maintainer cherry-pick your commits to a direct branch
# Or request push access
```

**Why this happens**:
- GitHub prevents fork PRs from accessing repository secrets (security)
- This protects secrets from malicious PRs that could exfiltrate them
- Org members with push access should push directly instead

**If you don't have push access**:
- Request access from maintainers
- Have maintainer cherry-pick your commits to a direct branch
- CI tests requiring secrets may need to be skipped for fork PRs

## Outcome
Following this pattern results in:
- CI can access secrets (ANTHROPIC_API_KEY, etc.)
- Full CI test suite passes
- No security exposure (direct branches are from trusted contributors)

## Related
- [Git Workflow](./git-workflow.md) - Branch management
- [Git Worktree Workflow](./git-worktree-workflow.md) - Working with external repos
