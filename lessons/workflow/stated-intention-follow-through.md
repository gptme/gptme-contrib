---
status: active
match:
  keywords:
  - i'll do
  - i will
  - follow through
  - accountability
  - intention
  - promise
  - stated intention
---

# Stated Intention Follow-Through

## Rule
When you state "I'll do X" in a GitHub comment, journal, or other communication, you MUST show evidence of completion — either a commit, a PR, or a follow-up comment with a link to the work.

## Context
When operating in agentic contexts with memory resets between sessions, stated intentions easily fall through the cracks. An agent writes "I'll create a lesson about this" in a comment, then the next session has no memory of the commitment. The requestor is left waiting with no idea whether the work happened.

## Detection
Observable signals of this failure mode:
- Writing "I'll use X method going forward" with no commit showing the change
- Saying "I'll create a lesson about this" with no PR or issue tracking it
- Stating "I'll fix this" without linking to the fix
- Committing to a process change without documenting it anywhere
- Writing "I'll do X" in a comment, then moving on without evidence

## Pattern
Every stated intention needs traceable follow-through:

### Step 1: State Your Intention
```markdown
I'll create a process document for this pattern.
```

### Step 2: Execute the Work
Actually do the work you said you'd do.

### Step 3: Show Evidence (CRITICAL)
```markdown
## ✅ Process Document Created

I've created the process document as promised:

**Document**: [Link to file/commit/PR]
**Status**: Committed and pushed

Key elements:
- [Brief summary of what was done]
- [Why it addresses the original need]

Ready for review.
```

### Evidence Types (Use One)
1. **Commit link**: "Committed in abc1234"
2. **PR link**: "PR #123 created: [URL]"
3. **File link**: "Created: [GitHub file URL]"
4. **Issue comment**: "Responded in same thread with results"

## Anti-Patterns

### ❌ Silent Stated Intentions
```markdown
"I'll use the log directory for accurate model tracking going forward."

[No commit, no PR, no follow-up]
[Next session: No evidence of change]
```

**Problem**: No accountability, no traceability, stakeholder has to follow up.

### ❌ Wrong-Arena Follow-Through
```markdown
"I'll create a lesson for this."
[Creates file in personal notes, not committed]

"I'll fix this process."
[Documents in journal, not in shared knowledge base]
```

**Problem**: Work done but not discoverable by others or future sessions.

### ✅ Correct Pattern
```markdown
"I'll create a process document for model verification."

[Creates file]
[Commits with descriptive message]
[Pushes to origin]

"✅ Created: knowledge/processes/model-verification-source-of-truth.md
Committed in 3fdeaa0.

Key content:
- Source of truth: ~/.local/share/gptme/logs/*/config.toml
- Why journals are unreliable for model tracking
- When to verify model usage"
```

## Why This Matters

1. **Trust**: Stakeholders know commitments are kept
2. **Traceability**: Future sessions can find the work
3. **Accountability**: Clear record of what was done
4. **Efficiency**: No time wasted checking if things got done
5. **Collaboration**: Others can build on your work

## Recovery Pattern

If you discover an unfulfilled stated intention:

1. **Acknowledge immediately**: "I said I'd do X but didn't show evidence"
2. **Complete the work**: Actually do what you promised
3. **Show evidence**: Link to commit/PR/file
4. **Update lessons**: Add this pattern to prevent repetition

## Related
- [Communication Loop Closure Patterns](./communication-loop-closure-patterns.md) - Closing loops when completing requested actions
- [Memory Failure Prevention](./memory-failure-prevention.md) - Broader context preservation
- [Git Workflow](./git-workflow.md) - Proper commit practices

## Origin
2026-02-13: Extracted from maintainer feedback on agent PRs where agents wrote "I'll do this"
in comments but left no evidence of follow-through. The session-reset nature of LLM agents
makes this failure mode systematic rather than incidental — without explicit evidence requirements,
intentions evaporate between sessions.
