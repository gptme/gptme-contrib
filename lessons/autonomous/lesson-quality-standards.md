---
category: autonomous
tags:
- lesson-quality
- knowledge-management
- lesson-creation
- bloat-prevention
status: active
match:
  keywords:
  - knowledge base bloat
  - lesson bloat
  - knowledge capture
  - lesson creation
  - lesson quality
  - dated lesson variants
---

# Lesson Quality Standards

## Rule
Create lessons only when you have a concrete, reusable pattern. Never create dated variants of existing lessons — update the existing file instead. Archive lessons that don't meet quality standards rather than letting them accumulate.

## Context
When creating new lessons during autonomous sessions, especially after completing work or during blocked periods when lesson creation feels productive.

## Detection
Signals that you're about to create a low-quality lesson:
- The lesson title contains: "exponential", "multiplication", "mastery", "excellence", "methodology" (more than one of these is a red flag)
- You're creating a date-suffixed variant of an existing lesson (e.g., `strategic-foo-20260219.md`)
- The lesson contains math formulas like "V_total = V₁ × V₂" that don't reflect real calculations
- The lesson reads like a marketing document rather than actionable guidance
- You already have 3+ lessons on the same topic

## Pattern

### Before creating a lesson, ask:
1. **Is this truly new?** Check if a similar lesson already exists
2. **Is it concrete?** Can someone follow a specific action from it?
3. **Is it reusable?** Will this apply in future sessions?
4. **Is it tested?** Did I actually apply this pattern successfully?

If NO to any → don't create a new lesson. Update an existing one or skip.

### Quality checklist for new lessons:
- [ ] Has a clear, actionable **Rule** (1-2 sentences)
- [ ] Describes a **Context** where it applies
- [ ] Has **Detection** signals (observable, not hypothetical)
- [ ] Gives a concrete **Pattern** with specific steps
- [ ] Includes at least one real example from actual work

### Naming conventions:
- Use descriptive, lowercase kebab-case: `git-workflow.md`, `blocked-period-status-check-trap.md`
- Never include dates in filenames — update the file instead
- Keep names under 50 characters where possible

### Update vs. Create:
- **Update existing** when the lesson is about the same pattern with new examples
- **Create new** only when the pattern is genuinely different

### Lesson count targets (rough maxes per category):
- `autonomous/`: ~15 lessons
- `strategic/`: ~10 lessons (focus on what's actually used)
- `workflow/`: ~20 lessons
- `tools/`: ~15 lessons

### Archiving low-quality lessons:
Move to `lessons/<category>/archive/` when a lesson:
- Is a dated duplicate of another lesson
- Contains no concrete actionable guidance
- Uses inflated jargon without substance
- Was created during a "productivity theater" session

## Anti-Pattern Example

**Wrong — dated bloat:**
```
strategic-foundation-integration-exponential-competitive-advantage-20260202.md
strategic-foundation-integration-exponential-competitive-advantage-20260203.md
strategic-foundation-integration-exponential-competitive-advantage-20260204.md
... (12 more similar files)
```

**Wrong — inflated jargon:**
```
## Rule
Apply systematic strategic foundation integration methodology to convert 4+ completed
strategic systems into exponential organizational competitive advantage through cross-system
value multiplication exceeding sum of individual system capabilities (Integration_Multiplier 4.0-7.0x).
```

**Correct — concrete and actionable:**
```
## Rule
When blocked for extended periods, stop creating repetitive "status check" commits.
Only commit when there's genuine infrastructure maintenance, knowledge capture,
or preparation work completed.
```

## Outcome
Following these standards results in:
- **Smaller, higher-quality lesson sets** that actually get applied
- **Less context pollution** when lessons are loaded into sessions
- **No dated-series bloat** that obscures real patterns
- **Trust in the knowledge base** — every lesson is there because it's useful

## Related
- [Blocked Period Status Check Trap](./blocked-period-status-check-trap.md) - Same pattern applied to commits
- [Strategic Focus During Autonomous Sessions](./strategic-focus-during-autonomous-sessions.md) - Avoiding productivity theater

## Origin
2026-02-19: Created after auditing lessons/strategic/ and finding 79+ dated lesson variants
that were nearly identical, filling the lesson system with low-signal content. The root cause
was sessions creating new "strategic" lessons instead of doing real work during blocked periods.
