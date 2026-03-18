---
match:
  keywords:
  - "conventional commits"
  - "commit message format"
  - "write commit message"
  - "git commit message"
status: active
---

# Git Commit Message Format

## Rule
All commit messages must follow Conventional Commits format.

## Context
When creating git commits in any workspace or repository.

## Detection
Observable signals indicating incorrect commit format:
- Commit message missing type prefix (feat, fix, docs, etc.)
- Vague descriptions ("update code", "fix stuff")
- Multiple unrelated changes in single commit
- Missing scope when relevant (e.g., "fix: bug" instead of "fix(api): bug")

## Pattern
```text
type(scope): short description

Optional longer description explaining why,
not just what changed.
```

**Common types**: feat, fix, docs, style, refactor, test, chore

**Example**:
```text
feat(auth): add JWT-based authentication

Implement token-based auth with refresh tokens.
Includes login/logout endpoints and middleware.
```

## Outcome
Following this pattern ensures:
- Automated versioning and changelog generation works
- Maintainable git history that's easy to review
- Consistent format across all repositories

## Related
- [Conventional Commits Specification](https://www.conventionalcommits.org/)

## Origin
Extracted from LOO effectiveness analysis in Bob's workspace (Δ=+0.162 session reward, p<0.001).
