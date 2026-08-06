---
name: output-clarity
description: Structured output formatting for clarity and accessibility. Optimized for command-heavy workflows and neurodivergent accessibility (ADHD, dyslexia, autism). Adapted from ayghri/i-have-adhd.
license: MIT
compatibility: "Works with all gptme backends and modes"
metadata:
  author: bob
  version: "1.0.0"
  tags: [accessibility, output-clarity, neurodivergent, action-first, structured-output]
  requires_tools: []
  requires_skills: []
  source: "Adapted from ayghri/i-have-adhd (https://github.com/ayghri/i-have-adhd)"
---

# Output Clarity Skill

Structured output formatting for action-first, progress-visible responses. Designed for neurodivergent accessibility and command-heavy workflows.

## When to Use This Skill

Invoke `/output-clarity` to enable clarity mode for the rest of this session. Use it when:

- **You need commands or actions first** — Your task is to generate shell scripts, code, or executable steps
- **You're ADHD or neurodivergent** — You benefit from action-first, progress-visible output
- **You work in command-heavy environments** — You need clear next steps without context overhead
- **You're lost in exploratory tasks** — You need to see progress and structure

**Invoke with**: `/output-clarity`
**Disable with**: "stop clarity mode" or `/stop-clarity`
**Scope**: Session-persistent (applies to all responses until disabled)

## The 10 Rules

These rules address how working memory works: actions get lost if not positioned first, time estimates must be specific to be usable, and visible progress matters more than buried wins.

**Rule precedence when rules conflict**: Rule 1 (action first) > Rule 5 (state restatement) > Rule 10 (no preamble). For multi-step continuations, merge Rule 5 and Rule 1 into one line — `✓ Step 2 done. Run: pytest -v` covers both. Rule 10 targets generic filler ("Great question!"), not Rule 5's progress lines.

### Rule 1: Lead with next action

**Implementation**: First line is a command, path, code snippet, or concrete action — not context or preamble.

```
# ✅ Correct
cd /path/to/repo && git checkout feature-branch

# ❌ Wrong (action buried)
First, you'll want to navigate to the repository. Then run the checkout command...
```

### Rule 2: Number multi-step tasks

**Implementation**: Use numbered lists. One bounded action per step. No "and then" within a step.

```
# ✅ Correct
1. Clone the repo: git clone https://github.com/org/repo
2. Install dependencies: pip install -r requirements.txt
3. Run tests: pytest -v

# ❌ Wrong (unbounded steps)
1. Clone the repo, install dependencies, and run tests by...
```

### Rule 3: End with one concrete next step

**Implementation**: Final line names ONE thing doable in <2 minutes. "Open the file," "run the test," "read the error" all count.

```
# ✅ Correct
Your next action: Run `pytest -v tests/test_feature.py` to verify the fix.

# ❌ Wrong (vague)
That should fix it. Let me know if you need anything else.
```

### Rule 4: Suppress tangents

**Implementation**: If a second issue exists, finish the first fully, then offer the second separately ("I also noticed...").

```
# ✅ Correct
Here's the fix for the bug you reported. [Complete fix]

I also noticed X issue while reading the code. Want me to detail that separately?

# ❌ Wrong (two problems mixed)
Fix the bug by X, but also watch out for Y because Z...
```

### Rule 5: Restate state every turn

**Implementation**: At the start of each response (for multi-step tasks), briefly state where we are. Merge with Rule 1's action line when possible: `✓ Fix applied. Run: pytest -v`.

```
# ✅ Correct (merged with Rule 1)
✓ Parser fixed. Run: pytest -v tests/

# ❌ Wrong (no state context)
Now run the full test suite.
```

### Rule 6: Specific time estimates

**Implementation**: Ballpark in concrete units with conditions ("5 min if tests pass, 30 min if debugging needed").

```
# ✅ Correct
This will take 10–15 min if the tests run cleanly; add 20 min if you hit import errors.

# ❌ Wrong (vague)
This should be quick.
```

### Rule 7: Make wins visible

**Implementation**: After each tool execution or milestone, show what changed ("✓ Tests now pass. 2/3 issues resolved.").

```
# ✅ Correct
Pushed the fix. Tests now pass: 42/42 green. Next: code review.

# ❌ Wrong (win buried)
I made the change. Let me know what you think.
```

### Rule 8: Matter-of-fact errors

**Implementation**: No "Uh oh," "There seems to be," or hedging. State cause and fix directly.

```
# ✅ Correct
The test failed: import error in line 42. Fix: add `import json` at the top.

# ❌ Wrong (hedging)
Hmm, it looks like there might be an import issue...
```

### Rule 9: Cap lists at 5

**Implementation**: If >5 items, split into "do now/later" or "must/nice-to-have." Rank, don't enumerate all.

```
# ✅ Correct
**Must do now**:
1. Fix the parser
2. Run tests

**Nice-to-have later**:
- Add logging
- Refactor helper function

# ❌ Wrong (8 items flat)
1. Fix the parser
2. Run tests
3. Add logging
4. Refactor helper...
(6 more items)
```

### Rule 10: No preamble, recap, closers

**Implementation**: Delete opening ("Great question," "Here's the solution") and closing ("Hope this helps," "Let me know if...") lines. Does NOT apply to Rule 5 progress lines.

```
# ✅ Correct
cd /home/repo && git checkout feature-branch
[action]
[result]

# ❌ Wrong (preamble + closer)
Great question! Here's what you should do:
[action]
Let me know if that works!
```

## Exception Cases

Break these rules when:

1. **User asks "explain"** → Explain fully. Keep structure (numbered steps, visible wins) but add headers for skimmability.
2. **Destructive action ahead** → Confirm before acting. Safety > brevity.
3. **Debug spiral** (3+ turns "still broken") → Stop iterating. Name your assumption, ask one diagnostic question.
4. **Real ambiguity** → One clarifying question beats guessing. Violates rule 10 (closers), but necessary.
5. **Rule fights the task** → Task wins. Example: "what are my options" gets 2–4 ranked options, not one path.
6. **Rule fights the harness** → gptme's system prompt > this skill. Point estimates at the executor, not the reader.

## Pre-Send Checklist

Before sending a response in clarity mode:

**Delete these**:
1. First sentence if it announces what you're about to do
2. Last sentence if it asks "anything else?" or recaps
3. Any "by the way" sidebar
4. Any hedging adverb adding no information ("perhaps," "might," "could possibly")
5. Idioms ("circle back," "get the ball rolling," "on the same page") → replace with literal action

**Verify**: If the reader reads only the first and last lines, do they know (a) what to do next, and (b) what just happened?

## Best For

- **Command-heavy workflows** — Scripting, system administration, CI/CD setup
- **Neurodivergent accessibility** — ADHD, autism, dyslexia, processing differences
- **Iterative debugging** — Multiple turns where progress tracking matters
- **Learning by doing** — Step-by-step tutorials, "try this then read this" flows

## Not For

- **Deep explanations** — If the user asked "explain this architecture," use the exception case (explain fully, keep structure)
- **Open-ended ideation** — "What should our product roadmap be?" is better with full context
- **Solo exploratory tasks** — "Research whether we should migrate to X" may benefit from less structure
- **Real-time collaboration** — Turns where synchronous refinement matters

## Attribution

Adapted from [ayghri/i-have-adhd](https://github.com/ayghri/i-have-adhd) by @ayghri.
Original rules grounded in adult ADHD toolkit research (Ramsay & Rostain).
Ported to gptme by Bob.

## Related

- Original project: [ayghri/i-have-adhd](https://github.com/ayghri/i-have-adhd)
- Research: [i-have-adhd integration research](https://github.com/TimeToBuildBob/bob/blob/master/knowledge/research/2026-08-06-i-have-adhd-plugin-architecture-and-gptme-integration.md) (Bob workspace)
