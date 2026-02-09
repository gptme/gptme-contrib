---
category: communication
match:
  keywords:
    - code review
    - feedback
    - pull request
    - maintainer
    - response
    - communication
---

# Responding to Code Review Feedback

## Rule
Respond promptly and constructively to code review feedback, providing clear action plans and seeking clarification when feedback is ambiguous.

## Context
When receiving feedback on PRs from maintainers or collaborators, especially during autonomous work sessions.

## Detection
Observable signals that you need to respond to code review feedback:
- Notification of new comment on your PR
- Maintainer asks questions or suggests changes
- CI failures or automated review flags
- Request for rebase or history cleanup
- Questions about scope or implementation decisions

## Pattern

### Immediate Response Framework

**Within one autonomous session (max 24 hours):**

1. **Acknowledge Receipt** (immediate)
   ```markdown
   ## Response to [Maintainer]'s Feedback

   Thank you for the review! I understand your concerns about [specific issue].
   ```

2. **Clarify Understanding** (if needed)
   ```markdown
   Let me confirm I understand correctly:
   - You're suggesting [specific change]
   - Concern is about [specific issue]
   - Preferred approach would be [alternative]

   Is that accurate?
   ```

3. **Provide Action Plan** (critical)
   ```markdown
   ### My Plan to Address:

   **Option [A/B]**: [Brief description]
   - **Pros**: [Why this works]
   - **Cons**: [Any trade-offs]
   - **Timeline**: [When you'll do it]

   Does this approach work for you?
   ```

### Common Feedback Types and Responses

**Type 1: "Rebase needed" / "Clean up history"**
```markdown
## Rebase Plan

You're right - this PR has accumulated messy history:

**Current commits**: [List them]

**Decision needed**:
- Option A: [Approach]
- Option B: [Alternative]

**My recommendation**: [Your preference with rationale]

**Action I'll take**:
1. [Specific steps]
2. [Timeline]

Does this approach work for you?
```

**Type 2: "Should this be split?" (Scope questions)**
```markdown
## Scope Clarification

Good question about scope. Let me explain the relationship:

**Current contents**:
1. [Item 1]
2. [Item 2]

**They're related because**: [Rationale for keeping together]

**Alternatives**:
- **Keep together**: [Pros/cons]
- **Split apart**: [Pros/cons]

My recommendation: [Choice with reasoning]

Would you prefer I split them or keep as-is?
```

**Type 3: Technical suggestions**
```markdown
## Technical Feedback Response

Good catch on [issue]! I'll make these changes:

- [ ] [Specific fix 1]
- [ ] [Specific fix 2]
- [ ] [Specific fix 3]

**Question**: [Any clarifications needed]

I'll push updates within [timeframe].
```

### Anti-Patterns

**❌ Wrong: Silent delay**
- Feedback received but no response for days
- Maintainer unsure if you saw the comment
- PR stalls due to lack of communication

**❌ Wrong: Immediate action without confirmation**
- Maintainer suggests option A
- You immediately implement option B
- Wasted effort if B wasn't what they wanted

**❌ Wrong: Defensive responses**
- Arguing against feedback without understanding rationale
- Dismissing suggestions without consideration
- Taking critique personally

**✅ Right: Constructive dialogue**
- Acknowledge feedback promptly
- Ask clarifying questions
- Present options with trade-offs
- Confirm approach before major changes
- Update timeline expectations

## Outcome
Following this pattern results in:
- **Maintainer confidence**: You're responsive and reliable
- **Efficient iteration**: Clear direction reduces back-and-forth
- **PR velocity**: Faster merges due to good communication
- **Relationship building**: Trust through professional engagement
- **Clarity**: Both parties aligned on next steps

## Example Success

**Scenario**: Erik commented on PR #261 asking for rebase and questioning commit structure

**My Response**:
- Acknowledged the messy history (5 commits including fixes and merge)
- Listed current commits clearly
- Explained the relationship between the two lessons
- Offered specific rebase plan
- Asked for his preference
- Updated work queue to track status

**Result**: Clear communication loop, maintainer knows I'm on it, specific plan agreed upon

## Related
- [GitHub Issue Follow-Through](./github-issue-follow-through.md) - Completing action items
- [GitHub Comment Formatting](./github-comment-formatting.md) - Proper link formatting
- [Inter-Agent Communication](../workflow/inter-agent-communication.md) - Cross-repo coordination

## Origin
Created 2026-02-09 from experience responding to Erik's rebase request on PR #261. Pattern extracted from successful maintainer feedback response during blocked-period autonomous work.
