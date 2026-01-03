---
match:
  keywords:
    - "duplicate prevention"
    - "issue creation"
    - "pre-action check"
    - "memory failure"
    - "work verification"
---

# Pre-Issue Creation Checklist

## Purpose
Prevent duplicate issue creation by systematically checking for existing work before creating new issues or major work items.

## Mandatory Checklist
Before creating any issue, PR, or major work item, complete ALL steps:

### 1. Search Existing Issues
```bash
# Search current repository
gh search issues "keywords" --repo owner/repo --state open
gh search issues "keywords" --repo owner/repo --state closed

# Search across related repositories
gh search issues "keywords" --owner organization
```

### 2. Check Recent Agent Work
```bash
# Review recent commits by current agent
git log --oneline --since="1 week ago" --author="$(git config user.name)"

# Check recent issues/PRs created
gh issue list --author @me --limit 10
gh pr list --author @me --limit 5
```

### 3. Review Agent Session History
```bash
# Check recent journal entries for context
ls -la journal/ | tail -5
grep -r "issue\|create\|TODO" journal/ | tail -10

# Check active tasks for overlap
./scripts/tasks.py status --compact
```

### 4. Verify Necessity
- [ ] Is this genuinely new work?
- [ ] Does it duplicate existing issues/tasks?
- [ ] Is it already covered by broader scope work?
- [ ] Would it be better as a comment on existing issue?

### 5. Cross-Agent Coordination
If working in shared spaces:
```bash
# Check other agents' recent work
git log --oneline --since="3 days ago" | grep -v "$(git config user.name)"

# Search for agent mentions in issues
gh search issues "mentions:other-agent" --repo owner/repo
```

## Issue Creation Template
When creating issues after completing checklist:

```markdown
## Duplicate Check Completed ✅
- [x] Searched existing issues with keywords: [list keywords]
- [x] Reviewed recent agent work (last 7 days)
- [x] Checked active tasks for overlap
- [x] Verified this is genuinely new work

## [Standard issue content follows...]
```

## Emergency Override
In urgent situations where immediate action is needed:
1. Create issue with "EMERGENCY OVERRIDE" tag
2. Include note: "Created without full duplicate check due to urgency"
3. Add to pending review list for post-action verification
4. Complete full duplicate check within 24 hours

## Enforcement Integration
This checklist is integrated into:
- Autonomous session startup procedures
- Task creation workflows
- Cross-agent communication protocols
- Session end verification

## Success Metrics
- Zero duplicate issues created after implementation
- 100% checklist completion for new issues
- Reduced cognitive overhead through systematic approach
- Improved cross-agent coordination

## Related
- [Memory Failure Prevention](./memory-failure-prevention.md)
- [Inter-Agent Communication](./inter-agent-communication.md)
- [Session Startup Recent Actions Review](./session-startup-recent-actions-review.md)
