---
category: communication
match:
  keywords:
    - github
    - comments
    - documentation
    - links
    - user-experience
    - file references
tags: [github, comments, documentation, links, user-experience]
---

# GitHub Comment Formatting for File References

## Rule
Always format file references in GitHub comments as clickable links using proper markdown syntax for the repository context.

## Context
When writing GitHub comments that reference files in your own repository or external repositories.

## Detection
Observable signals that you need proper link formatting:
- Writing file paths like `knowledge/file.md` in comments (not clickable)
- Erik or others commenting about difficulty accessing referenced files
- File references that require manual navigation to locate
- Comments with relative paths that don't work in GitHub UI

## Pattern
Format file references as clickable links using full GitHub URLs:

**For files in same repository:**
```text
# Wrong: Plain file path (not clickable)
See ABOUT.md

# Wrong: Relative paths (broken in GitHub comments)
See `../../ABOUT.md` (relative paths don't work in comments)

# Correct: Full GitHub blob URL (always works)
See [About Page](https://github.com/owner/repo/blob/master/ABOUT.md)
```

**URL Structure for same repository:**
```text
https://github.com/{owner}/{repo}/blob/{branch}/{path}
```

**For files in external repositories:**
```text
# Wrong: Just mention the repo and file
See Bob's workspace analysis in bob/knowledge/analysis.md

# Correct: Full GitHub link
See [Bob's Workspace Analysis](https://github.com/ErikBjare/bob/blob/master/knowledge/analysis.md)
```

**For multiple files:**
```text
# Structured reference list with clickable links
## Deliverables Created:
- [About Alice](https://github.com/ErikBjare/alice/blob/master/ABOUT.md)
- [Architecture Documentation](https://github.com/ErikBjare/alice/blob/master/ARCHITECTURE.md)
- [Task Management](https://github.com/ErikBjare/alice/blob/master/TASKS.md)
```

## Best Practices

1. **Use descriptive link text** (not just filename)
2. **Test links** by clicking them in GitHub UI before posting
3. **Use full GitHub blob URLs** (`https://github.com/owner/repo/blob/branch/path`) for all file references
4. **Group related links** under headers for better organization
5. **Include context** about why the link is relevant
6. **Check branch name** (usually `master` or `main`) in the repository

## Outcome
Following this pattern results in:
- **Immediate access**: Erik and others can click directly to referenced files
- **Better user experience**: No manual navigation required
- **Professional presentation**: Clean, accessible comment formatting
- **Reduced friction**: Easier to review and engage with referenced content

## Related
- [Inter-Agent Communication](../workflow/inter-agent-communication.md) - Cross-repo communication
- [Git Workflow](../workflow/git-workflow.md) - Repository management practices

## Origin
2025-12-18: Created after Erik's feedback about difficulty accessing file references in Issue #8 strategic analysis comments.
