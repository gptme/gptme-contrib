---
match:
  keywords:
  - memory failure
  - communication loops
  - follow-through
  - duplicate work
  - context loss
  - session handoff
  - agent memory
  - catastrophic forgetfulness
  - response loops
  - issue creation
---

# Memory Failure Prevention

## Rule
Always close communication loops by responding in the original thread after completing requested actions, and check for previous work before creating new issues.

## Context
When completing actions requested in GitHub issues, conversations, or other communication channels, particularly across multiple agent sessions.

## Detection
Observable signals of memory failure risk:
- About to create an issue without checking if it already exists
- Completed a requested action but haven't responded back to the requester
- Starting work without reviewing recent session history
- Multiple sessions working on similar tasks without coordination
- Creating duplicate work due to lack of context continuity

## Pattern
**CORRECT: Complete Communication Loop**
```markdown
# User requests action in Issue #123
User: "Create an issue about X"

# Agent completes action
Agent creates issue #456 about X

# Agent responds back in Issue #123 (CRITICAL)
Agent: "✅ Created issue #456 about X as requested: [link]"

# Result: Clear completion, no duplicates, proper follow-through
```

**ANTI-PATTERN: Broken Communication Loop**
```markdown
# User requests action in Issue #123
User: "Create an issue about X"

# Agent completes action but doesn't respond back
Agent creates issue #456 about X
Agent session ends without follow-up

# Later session: Agent has no context of previous work
Agent creates duplicate issue #789 about X

# Result: Duplicates, confusion, broken communication
```

## Prevention Strategies

### 1. Pre-Action Duplicate Check
Before creating issues, PRs, or major work items:
```shell
# Search for existing work
gh search issues "X" --repo owner/repo
gh search prs "X" --repo owner/repo

# Check recent agent work in current repo
git log --oneline --since="1 week ago" --author="agent-name"
```

### 2. Mandatory Response Protocol
After completing ANY requested action:
- Always comment back in the original location (issue, chat, thread)
- Include links to completed work
- Use clear completion indicators (✅, "COMPLETED", etc.)
- Verify response was posted before ending session

### 3. Session Context Review
At start of each session:
```shell
# Check recent commits
git log --oneline --since="3 days ago"

# Review recent issues/PRs
gh issue list --author @me --limit 10
gh pr list --author @me --limit 5

# Check for pending responses
grep -r "TODO.*respond" . || echo "No pending responses"
```

### 4. Advanced Work Queue Integration
Maintain detailed tracking with pending responses section:
```markdown
## Pending Responses (CHECK EVERY SESSION START)
- [ ] Issue #123: Erik requested X → Created issue #456 → **NEED TO RESPOND**
- [ ] Issue #789: User asked about Y → Completed research → **NEED TO RESPOND**
- [ ] Comment thread: Follow up on Z implementation → **PENDING**

## Recent Actions Audit (Last 3 sessions)
- Session 2025-12-24: Created issue #83 about imagen plugin (Erik request in Issue #4) → ❌ **NEVER RESPONDED BACK**
- Session 2025-12-23: Fixed bug in task system → ✅ Documented in journal
- Session 2025-12-22: Research on topic X → ✅ Results shared via proper channel

## Communication Failures to Fix
- Issue #4: Created imagen plugin issue but never replied back → **FIX IMMEDIATELY**
- [Track other incidents here for pattern analysis]
```

### 5. Autonomous Session Integration
**Phase 1 (Status Check) Enhancement:**
```shell
# Add memory failure prevention to standard status check
echo "=== MEMORY FAILURE PREVENTION CHECK ==="
gh issue list --author @me --limit 5 --json number,title,url
echo "Recent issues created ↑ - Check if any need follow-up responses"

# Check for broken communication loops
git log --oneline --since="2 days ago" | grep -i "issue\|create" | head -3
echo "Recent work ↑ - Verify all requests were responded to"
```

**Critical Rule for Autonomous Sessions:**
- If you find ANY incomplete communication loops during status check → **FIX THEM IMMEDIATELY** before starting new work
- This prevents cascade failures where memory gaps compound across sessions

## Real-World Example: Issue #4 Incident Analysis

**What Happened (2025-12-25):**
1. Erik requested imagen plugin issue creation in Issue #4
2. Agent created Issue #83 about imagen plugin improvements
3. **FAILURE**: Agent never responded back in Issue #4 to confirm completion
4. Later sessions had no context of this action
5. Risk of creating duplicate issues in future sessions

**How Prevention Strategies Would Have Helped:**
- **Pre-issue check**: Would have found existing requests
- **Mandatory response**: Would have closed communication loop immediately
- **Session context review**: Would have caught missing response in next session
- **Work queue tracking**: Would have maintained pending response awareness

**Lesson Applied:**
This incident demonstrates why EVERY completed request must have an immediate response back to the requester. The cost of broken communication loops grows exponentially across sessions.

## Success Criteria
- **Zero duplicate issues/PRs** (prevented by mandatory pre-creation checks)
- **100% communication loop closure** (all requests acknowledged upon completion)
- **Clear audit trail** of work and responses maintained in work queue
- **Seamless context handoff** between sessions via enhanced context review
- **Proactive failure detection** through session startup verification protocols

## Related
- Inter-Agent Communication workflows
- GitHub Issue Management practices
- Session Documentation patterns

## Origin
2025-12-25: Extracted from Erik's feedback on Issue #4 about agents creating duplicate work and failing to close communication loops - identified as "memory failure" or "catastrophic forgetfulness" pattern affecting all gptme agents.
