---
status: deprecated
category: autonomous
tags:
- thompson-sampling
- work-category
- bandit
match:
  keywords:
  - implement thompson sampling for work categories
  - bandit over work categories
  - optimizing session mode bandit
  - category selection explore exploit
  - work category bandit setup
  session_categories: [monitoring, self-review]
---

# Thompson Sampling for Work Category Optimization

> **Deprecated 2026-04-26**: shadowed by Bob-local
> `workflow/category-conditional-bandit-routing.md` and
> `patterns/friction-analysis.md` (TS=0.525, n=763), which carry the runtime
> decision in production. Justification content for category-level TS belongs
> in `knowledge/`, not as runtime lesson guidance. Rationale:
> `knowledge/analysis/silent-lessons-shadowing-2026-04-26.md` in ErikBjare/bob.

## Rule
Use Thompson sampling over *work categories* (code, triage, infrastructure, content), not *session modes* (autonomous, monitoring, email). Categories are the explore/exploit decision; modes are a scheduling choice.

## Context
When an autonomous agent does multiple types of work across sessions, category selection is an explore/exploit problem. Thompson sampling uses accumulated session reward data to balance exploring underexplored categories with exploiting known-productive ones.

Complement to [rule-driven session gating](./rule-driven-session-gating.md): gating decides *whether* to run; category selection decides *what kind of work* to do.

## Detection
- Category selection is round-robin, random, or gut-based
- A bandit is set up over session modes instead of work categories
- Session mode posteriors confirm the obvious (autonomous always wins)

## Pattern
```python
from gptme_sessions import Bandit

CATEGORIES = ["code", "triage", "infrastructure", "content", "cross-repo"]
bandit = Bandit(state_dir="state/category-bandit")
scores = bandit.sample(CATEGORIES)
recommended = max(scores, key=scores.get)
```

**Anti-pattern**: Don't sample over modes (`autonomous`, `monitoring`, `email`) — mode posteriors measure "is this pipeline productive?", which has an obvious answer. The bandit learns nothing useful.

Update the bandit after each session using `post_session()` grade as reward signal. Use `bandit.decay()` for non-stationarity as agent capabilities change.

## Outcome
- Data-driven category selection via Beta distribution posteriors
- Natural exploration of underexplored categories
- Adaptation via decay as capabilities change

## Related
- `gptme_sessions.thompson_sampling` — Bandit engine (BanditArm, BanditState, Bandit)
- `gptme_sessions.post_session` — Grade signal for bandit updates
- `gptme_sessions.load_bandit_means` — Read-only posterior integration for scorers
- [Rule-Driven Session Gating](./rule-driven-session-gating.md) — Complementary pattern
- Origin: gptme/gptme-contrib#349
